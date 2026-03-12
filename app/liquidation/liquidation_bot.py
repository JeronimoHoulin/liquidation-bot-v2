"""
EVault Liquidation Bot
"""
import threading
import random
import time
import queue
import os
import json
import sys
import math

from concurrent.futures import ThreadPoolExecutor
from typing import Tuple, Dict, Any, Optional

from web3 import Web3
from web3.logs import DISCARD


from app.liquidation.utils import (setup_logger,
                   create_contract_instance,
                   make_api_request,
                   global_exception_handler,
                   post_liquidation_opportunity_on_slack,
                   post_liquidation_result_on_slack,
                   post_low_health_account_report,
                   post_unhealthy_account_on_slack,
                   post_error_notification,
                   get_eth_usd_quote,
                   get_btc_usd_quote)

from app.liquidation.config_loader import ChainConfig

### ENVIRONMENT & CONFIG SETUP ###
logger = setup_logger()
sys.excepthook = global_exception_handler

# If set, only print health/liquidation output for accounts owned by this address.
# Useful for debugging a specific user's sub-accounts without noise from others.
# Set via env var DEBUG_OWNER or hardcode here. Clear to monitor all accounts.
DEBUG_OWNER = os.environ.get("DEBUG_OWNER", "0x744cee96E39852C82D877eb329378Fcdf99e83F1").lower()


### MAIN CODE ###

def _fmt_uoa(value: int, unit_of_account: str, config) -> str:
    """Format a raw unit-of-account integer to a human-readable string."""
    amount = value / 1e18
    if unit_of_account == config.WETH:
        return f"{amount:.8f} ETH"
    if unit_of_account == config.USD:
        return f"${amount:.8f}"
    return f"{amount:.8f}"


class Vault:
    """
    Represents a vault in the EVK System.
    This class provides methods to interact with a specific vault contract.
    This does not need to be serialized as it does not store any state
    """
    def __init__(self, address, config: ChainConfig):
        self.config = config
        
        self.address = address
        self.instance = create_contract_instance(address, self.config.EVAULT_ABI_PATH, self.config)

        self.underlying_asset_address = self.instance.functions.asset().call()
        self.vault_name = self.instance.functions.name().call()
        self.vault_symbol = self.instance.functions.symbol().call()

        self.unit_of_account = self.instance.functions.unitOfAccount().call()
        self.oracle_address = self.instance.functions.oracle().call()

        self.pyth_feed_ids = []
        self.last_pyth_feed_ids_update = 0

    def get_account_liquidity(self, account_address: str) -> Tuple[int, int]:
        """
        Get liquidity metrics for a given account.

        Args:
            account_address (str): The address of the account to check.

        Returns:
            Tuple[int, int]: A tuple containing (collateral_value, liability_value).
        """
        try:
            balance = self.instance.functions.balanceOf(
                Web3.to_checksum_address(account_address)).call()
        except Exception as ex: # pylint: disable=broad-except
            logger.error("Vault: Failed to get balance for account %s: %s",
                         account_address, ex, exc_info=True)
            return (0, 0, 0)

        try:
            if time.time() - self.last_pyth_feed_ids_update > self.config.PYTH_CACHE_REFRESH:
                self.pyth_feed_ids = PullOracleHandler.get_feed_ids(self, self.config)
                self.last_pyth_feed_ids_update = time.time()

            if len(self.pyth_feed_ids) > 0:
                collateral_value, liability_value = PullOracleHandler.get_account_values_with_pyth_batch_simulation(
                    self, account_address, self.pyth_feed_ids, self.config)
            else:
                (collateral_value, liability_value) = self.instance.functions.accountLiquidity(
                    Web3.to_checksum_address(account_address),
                    True
                ).call()
        except Exception as ex: # pylint: disable=broad-except
            if ex.args[0] != "0x43855d0f" and ex.args[0] != "0x6d588708":
                logger.error("Vault: Failed to get account liquidity"
                            " for account %s: Contract error - %s",
                            account_address, ex)
                # return (balance, 100, 100)
            return (balance, 0, 0)

        return (balance, collateral_value, liability_value)

    def check_liquidation(self,
                          borower_address: str,
                          collateral_address: str,
                          liquidator_address: str
                          ) -> Tuple[int, int]:
        """
        Call checkLiquidation on EVault for an account

        Args:
            borower_address (str): The address of the borrower.
            collateral_address (str): The address of the collateral asset.
            liquidator_address (str): The address of the potential liquidator.

        Returns:
            Tuple[int, int]: A tuple containing (max_repay, seized_collateral).
        """
        if len(self.pyth_feed_ids) > 0:
            (max_repay, seized_collateral) = PullOracleHandler.check_liquidation_with_pyth_batch_simulation(
                self,
                Web3.to_checksum_address(liquidator_address),
                Web3.to_checksum_address(borower_address),
                Web3.to_checksum_address(collateral_address),
                self.pyth_feed_ids,
                self.config
                )
        else:
            (max_repay, seized_collateral) = self.instance.functions.checkLiquidation(
                Web3.to_checksum_address(liquidator_address),
                Web3.to_checksum_address(borower_address),
                Web3.to_checksum_address(collateral_address)
                ).call()
        return (max_repay, seized_collateral)

    def convert_to_assets(self, amount: int) -> int:
        """
        Convert an amount of vault shares to underlying assets.

        Args:
            amount (int): The amount of vault tokens to convert.

        Returns:
            int: The amount of underlying assets.
        """
        return self.instance.functions.convertToAssets(amount).call()

    def get_ltv_list(self):
        """
        Return list of LTVs for this vault
        """
        return self.instance.functions.LTVList().call()

    def ltv_liquidation(self, collateral_address: str) -> int:
        """
        Return the liquidation LTV for a given collateral vault (uint16, 0-10000 scale where 10000=100%).
        """
        return self.instance.functions.LTVLiquidation(
            Web3.to_checksum_address(collateral_address)
        ).call()

class Account:
    """
    Represents an account in the EVK System.
    This class provides methods to interact with a specific account and
    manages individual account data, including health scores,
    liquidation simulations, and scheduling of updates. It also provides
    methods for serialization and deserialization of account data.
    """
    def __init__(self, address, controller: Vault, config: ChainConfig):
        self.config = config
        self.address = address
        self.owner, self.subaccount_number = EVCListener.get_account_owner_and_subaccount_number(self.address, config)
        self.controller = controller
        self.time_of_next_update = time.time()
        self.current_health_score = math.inf
        self.balance = 0
        self.value_borrowed = 0
        self.collateral_value = 0
        self.liability_value = 0


    def update_liquidity(self) -> float:
        """
        Update account's liquidity & next scheduled update and return the current health score.

        Returns:
            float: The updated health score of the account.
        """
        self.get_health_score()
        self.get_time_of_next_update()

        return self.current_health_score


    def get_health_score(self) -> float:
        """
        Calculate and return the current health score of the account.

        Returns:
            float: The current health score of the account.
        """

        balance, collateral_value, liability_value = self.controller.get_account_liquidity(
            self.address)
        self.balance = balance

        self.value_borrowed = liability_value
        if self.controller.unit_of_account == self.config.WETH:
            self.value_borrowed = get_eth_usd_quote(liability_value, self.config)
        elif self.controller.unit_of_account == self.config.BTC:
            self.value_borrowed = get_btc_usd_quote(liability_value, self.config)

        # Special case for 0 values on balance or liability
        if liability_value == 0:
            self.current_health_score = math.inf
            return self.current_health_score


        self.current_health_score = collateral_value / liability_value
        self.collateral_value = collateral_value
        self.liability_value = liability_value

        if not DEBUG_OWNER or self.owner.lower() == DEBUG_OWNER:
            uoa = self.controller.unit_of_account
            ltv_pct = (liability_value / collateral_value * 100) if collateral_value > 0 else float('inf')
            status = "UNHEALTHY (LTV>LLTV) !!!" if self.current_health_score < 1 else "healthy"
            print(f"[HEALTH] {self.address} | health={self.current_health_score:.8f} | LTV={ltv_pct:.4f}% | "
                  f"debt={_fmt_uoa(liability_value, uoa, self.config)} | "
                  f"coll={_fmt_uoa(collateral_value, uoa, self.config)} | {status}")
        return self.current_health_score

    def get_time_of_next_update(self) -> float:
        """
        Calculate the time of the next update for this account based on size and health score.

        Returns:
            float: The timestamp of the next scheduled update.
        """
        # Special case for infinite health score
        if self.current_health_score == math.inf:
            self.time_of_next_update = -1
            return self.time_of_next_update

        # Determine size category
        if self.value_borrowed < self.config.TEENY:
            size_prefix = "TEENY"
        elif self.value_borrowed < self.config.MINI:
            size_prefix = "MINI"
        elif self.value_borrowed < self.config.SMALL:
            size_prefix = "SMALL"
        elif self.value_borrowed < self.config.MEDIUM:
            size_prefix = "MEDIUM"
        else:
            size_prefix = "LARGE"  # For anything >= MEDIUM

        # Get the appropriate time values based on size
        liq_time = getattr(self.config, f"{size_prefix}_LIQ")
        high_risk_time = getattr(self.config, f"{size_prefix}_HIGH")
        safe_time = getattr(self.config, f"{size_prefix}_SAFE")

        # Calculate time gap based on health score
        if self.current_health_score < self.config.HS_LIQUIDATION:
            time_gap = liq_time
        elif self.current_health_score < self.config.HS_HIGH_RISK:
            # Linear interpolation between liq and high_risk times
            ratio = (self.current_health_score - self.config.HS_LIQUIDATION) / (self.config.HS_HIGH_RISK - self.config.HS_LIQUIDATION)
            time_gap = liq_time + (high_risk_time - liq_time) * ratio
        elif self.current_health_score < self.config.HS_SAFE:
            # Linear interpolation between high_risk and safe times
            ratio = (self.current_health_score - self.config.HS_HIGH_RISK) / (self.config.HS_SAFE - self.config.HS_HIGH_RISK)
            time_gap = high_risk_time + (safe_time - high_risk_time) * ratio
        else:
            time_gap = safe_time

        # Randomly adjust time by ±10% to avoid synchronized checks
        time_of_next_update = time.time() + time_gap * random.uniform(0.9, 1.1)

        # Keep existing next update if it's already scheduled between now and calculated time
        if not(self.time_of_next_update < time_of_next_update and self.time_of_next_update > time.time()):
            self.time_of_next_update = time_of_next_update

        return self.time_of_next_update


    def simulate_liquidation(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Simulate liquidation of this account to determine if it's profitable.

        Returns:
            Tuple[bool, Optional[Dict[str, Any]]]: A tuple containing a boolean indicating
            if liquidation is profitable, and a dictionary with liquidation details if profitable.
        """
        result = Liquidator.simulate_liquidation(self.controller, self.address, self, self.config)
        return result

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert the account object to a dictionary representation.

        Returns:
            Dict[str, Any]: A dictionary representation of the account.
        """
        return {
            "address": self.address,
            "controller_address": self.controller.address,
            "time_of_next_update": self.time_of_next_update,
            "current_health_score": self.current_health_score
        }

    @staticmethod
    def from_dict(data: Dict[str, Any], vaults: Dict[str, Vault], config: ChainConfig) -> "Account":
        """
        Create an Account object from a dictionary representation.

        Args:
            data (Dict[str, Any]): The dictionary representation of the account.
            vaults (Dict[str, Vault]): A dictionary of available vaults.

        Returns:
            Account: An Account object created from the provided data.
        """
        controller = vaults.get(data["controller_address"])
        if not controller:
            controller = Vault(data["controller_address"], config)
            vaults[data["controller_address"]] = controller
        account = Account(address=data["address"], controller=controller, config=config)
        account.time_of_next_update = data["time_of_next_update"]
        account.current_health_score = data["current_health_score"]
        return account

class AccountMonitor:
    """
    Primary class for the liquidation bot system.

    This class is responsible for maintaining a list of accounts, scheduling
    updates, triggering liquidations, and managing the overall state of the
    monitored accounts. It also handles saving and loading the monitor's state.
    """
    def __init__(self, chain_id: int, config: ChainConfig, notify = False, execute_liquidation = False):
        self.chain_id = chain_id
        self.w3 = config.w3,
        self.config = config
        self.accounts = {}
        self.vaults = {}
        self.update_queue = queue.PriorityQueue()
        self.condition = threading.Condition()
        self.executor = ThreadPoolExecutor(max_workers=32)
        self.running = True
        self.latest_block = 0
        self.last_saved_block = 0
        self.notify = notify
        self.execute_liquidation = execute_liquidation

        self.recently_posted_low_value = {}

    def start_queue_monitoring(self) -> None:
        """
        Start monitoring the account update queue.
        This is the main entry point for the account monitor.
        """
        save_thread = threading.Thread(target=self.periodic_save)
        save_thread.start()

        if self.notify:
            low_health_report_thread = threading.Thread(target=
                                                        self.periodic_report_low_health_accounts)
            low_health_report_thread.start()

        while self.running:
            with self.condition:
                while self.update_queue.empty():
                    self.condition.wait()

                next_update_time, address = self.update_queue.get()

                # check for special value that indicates
                # account should be skipped & removed from queue
                if next_update_time == -1:
                    continue

                current_time = time.time()
                if next_update_time > current_time:
                    self.update_queue.put((next_update_time, address))
                    self.condition.wait(next_update_time - current_time)
                    continue

                self.executor.submit(self.update_account_liquidity, address)


    def update_account_on_status_check_event(self, address: str, vault_address: str) -> None:
        """
        Update an account based on a status check event.

        Args:
            address (str): The address of the account to update.
            vault_address (str): The address of the vault associated with the account.
        """

        # Skip vaults not in the allowlist (when allowlist is configured)
        allowlist = self.config.VAULT_ALLOWLIST
        if allowlist and Web3.to_checksum_address(vault_address) not in allowlist:
            return

        # If the vault is not already tracked in the list, create it
        if vault_address not in self.vaults:
            self.vaults[vault_address] = Vault(vault_address, self.config)

        vault = self.vaults[vault_address]

        # If the account is not in the list or the controller has changed, add it to the list
        if (address not in self.accounts or
            self.accounts[address].controller.address != vault_address):
            account = Account(address, vault, self.config)
            self.accounts[address] = account
            print(f"[MONITOR] Account added: {address} | Vault: {vault.vault_symbol} ({vault_address})")

        self.update_account_liquidity(address)

    def update_account_liquidity(self, address: str) -> None:
        """
        Update the liquidity of a specific account.

        Args:
            address (str): The address of the account to update.
        """
        try:
            account = self.accounts.get(address)

            if not account:
                logger.error("AccountMonitor: %s not found in account list.",
                             address, exc_info=True)
                return

            prev_scheduled_time = account.time_of_next_update

            health_score = account.update_liquidity()

            if DEBUG_OWNER and account.owner.lower() != DEBUG_OWNER:
                # silently re-queue without printing liquidation details
                next_update_time = account.time_of_next_update
                if next_update_time != prev_scheduled_time:
                    with self.condition:
                        self.update_queue.put((next_update_time, address))
                        self.condition.notify()
                return

            if health_score < 1:
                try:
                    uoa = account.controller.unit_of_account
                    ltv_pct = (account.liability_value / account.collateral_value * 100) if account.collateral_value > 0 else float('inf')
                    shortfall = account.liability_value - account.collateral_value

                    # Fetch debt token amount + oracle price for detailed logging
                    try:
                        debt_underlying = account.controller.instance.functions.debtOf(
                            Web3.to_checksum_address(address)).call()
                        debt_erc20 = create_contract_instance(
                            account.controller.underlying_asset_address, self.config.ERC20_ABI_PATH, self.config)
                        d_dec = debt_erc20.functions.decimals().call()
                        d_sym = debt_erc20.functions.symbol().call()
                        debt_token_str = f"{debt_underlying / (10**d_dec):.6f} {d_sym}"
                        oracle = create_contract_instance(
                            account.controller.oracle_address, self.config.ORACLE_ABI_PATH, self.config)
                        debt_price_raw = oracle.functions.getQuote(
                            10**d_dec,
                            Web3.to_checksum_address(account.controller.underlying_asset_address),
                            Web3.to_checksum_address(account.controller.unit_of_account)
                        ).call()
                        debt_price_str = _fmt_uoa(debt_price_raw, uoa, self.config)
                    except Exception:  # pylint: disable=broad-except
                        debt_token_str = "N/A"
                        debt_price_str = "N/A"

                    print(f"\n{'='*70}")
                    print(f"[!!!] UNHEALTHY ACCOUNT — LTV > LLTV (LIQUIDATION ZONE)")
                    print(f"{'='*70}")
                    print(f"  Sub-account  : {address}")
                    print(f"  Owner        : {account.owner}  (sub #{account.subaccount_number})")
                    print(f"  Debt vault   : {account.controller.vault_symbol} ({account.controller.address})")
                    print(f"  ── Position ──────────────────────────────────────────────")
                    print(f"  Health score : {health_score:.8f}  (< 1.0 = undercollateralised)")
                    print(f"  LTV          : {ltv_pct:.4f}%")
                    print(f"  Collateral   : {_fmt_uoa(account.collateral_value, uoa, self.config)}")
                    print(f"  Debt (value) : {_fmt_uoa(account.liability_value, uoa, self.config)}")
                    print(f"  Shortfall    : {_fmt_uoa(shortfall, uoa, self.config)}")
                    print(f"  ── Debt asset ────────────────────────────────────────────")
                    print(f"  Amount owed  : {debt_token_str}  (raw: {debt_underlying if debt_token_str != 'N/A' else 'N/A'})")
                    print(f"  Oracle price : {debt_price_str} per token")
                    print(f"{'='*70}\n")

                    if self.notify:
                        if account.address in self.recently_posted_low_value:
                            if (time.time() - self.recently_posted_low_value[account.address]
                                < self.config.LOW_HEALTH_REPORT_INTERVAL
                                and account.value_borrowed < self.config.SMALL_POSITION_THRESHOLD):
                                pass
                        else:
                            try:
                                post_unhealthy_account_on_slack(address, account.controller.address,
                                                                health_score,
                                                                account.value_borrowed, self.config)
                                if account.value_borrowed < self.config.SMALL_POSITION_THRESHOLD:
                                    self.recently_posted_low_value[account.address] = time.time()
                            except Exception as ex: # pylint: disable=broad-except
                                logger.error("AccountMonitor: "
                                             "Failed to post low health notification "
                                             "for account %s to slack: %s",
                                             address, ex, exc_info=True)

                    print(f"[LIQ] Simulating liquidation for {address}...")
                    (result, liquidation_data, params) = account.simulate_liquidation()

                    if result:
                        max_repay = liquidation_data['leftover_borrow']
                        seized = liquidation_data['seized_shares']
                        underlying = liquidation_data['underlying_collateral_received']
                        coll_addr = liquidation_data['collateral_address']
                        MAX_UINT256 = 2**256 - 1

                        # Fetch collateral token details for readable output
                        try:
                            coll_vault_sym = liquidation_data.get('collateral_vault_symbol', coll_addr[:10])
                            coll_erc20 = create_contract_instance(
                                liquidation_data['collateral_asset'], self.config.ERC20_ABI_PATH, self.config)
                            c_dec = coll_erc20.functions.decimals().call()
                            c_sym = coll_erc20.functions.symbol().call()
                            seize_str = f"{underlying / (10**c_dec):.6f} {c_sym}"
                            d_erc20 = create_contract_instance(
                                account.controller.underlying_asset_address, self.config.ERC20_ABI_PATH, self.config)
                            d_dec = d_erc20.functions.decimals().call()
                            d_sym = d_erc20.functions.symbol().call()
                            repay_str = f"{max_repay / (10**d_dec):.6f} {d_sym}"
                        except Exception:  # pylint: disable=broad-except
                            seize_str = f"{underlying} (raw)"
                            repay_str  = f"{max_repay} (raw)"

                        print(f"\n{'='*70}")
                        print(f"[!!!] PROFITABLE LIQUIDATION FOUND")
                        print(f"{'='*70}")
                        print(f"  Violator         : {address}")
                        print(f"  Debt vault       : {account.controller.vault_symbol} ({account.controller.address})")
                        print(f"  Collateral vault : {coll_addr}")
                        print(f"  ── Expected outcome ──────────────────────────────────────")
                        print(f"  Max repay        : {repay_str}  (raw: {max_repay})")
                        print(f"  Seize (shares)   : {seized}")
                        print(f"  Seize (tokens)   : {seize_str}  (raw: {underlying})")
                        print(f"  Shortfall closed : {_fmt_uoa(account.liability_value - account.collateral_value, account.controller.unit_of_account, self.config)}")
                        print(f"  ── Call parameters ───────────────────────────────────────")
                        print(f"  vault.liquidate(")
                        print(f"      violator        = {address}")
                        print(f"      collateral      = {coll_addr}")
                        print(f"      repayAssets     = {MAX_UINT256}  (MAX_UINT256)")
                        print(f"      minYieldBalance = 0")
                        print(f"  )")
                        print(f"{'='*70}\n")

                        if self.notify:
                            try:
                                post_liquidation_opportunity_on_slack(address,
                                                                      account.controller.address,
                                                                      liquidation_data, params, self.config)
                            except Exception as ex: # pylint: disable=broad-except
                                logger.error("AccountMonitor: "
                                             "Failed to post liquidation notification "
                                             " for account %s to slack: %s",
                                             address, ex, exc_info=True)
                        if self.execute_liquidation:
                            try:
                                tx_hash, tx_receipt = Liquidator.execute_liquidation(
                                    liquidation_data["tx"], self.config,
                                    liquidation_data.get("vault_instance"))
                                if tx_hash and tx_receipt:
                                    print(f"[!!!] LIQUIDATION EXECUTED | tx: {tx_hash}")
                                    if self.notify:
                                        try:
                                            post_liquidation_result_on_slack(address,
                                                                            account.controller.address,
                                                                            liquidation_data,
                                                                            tx_hash, self.config)
                                        except Exception as ex: # pylint: disable=broad-except
                                            logger.error("AccountMonitor: "
                                                "Failed to post liquidation result "
                                                " for account %s to slack: %s",
                                                address, ex, exc_info=True)

                                # Update account health score after liquidation
                                account.update_liquidity()
                            except Exception as ex: # pylint: disable=broad-except
                                logger.error("AccountMonitor: "
                                             "Failed to execute liquidation for account %s: %s",
                                             address, ex, exc_info=True)
                    else:
                        print(f"[LIQ] {address} — unhealthy but not currently liquidatable (see above)")
                except Exception as ex: # pylint: disable=broad-except
                    logger.error("AccountMonitor: "
                                 "Exception simulating liquidation for account %s: %s",
                                 address, ex, exc_info=True)

            next_update_time = account.time_of_next_update

            # if next update hasn't changed, means we already have a check scheduled
            if next_update_time == prev_scheduled_time:
                return

            with self.condition:
                self.update_queue.put((next_update_time, address))
                self.condition.notify()

        except Exception as ex: # pylint: disable=broad-except
            logger.error("AccountMonitor: Exception updating account %s: %s",
                         address, ex, exc_info=True)

    def save_state(self, local_save: bool = True) -> None:
        """
        Save the current state of the account monitor.

        Args:
            local_save (bool, optional): Whether to save the state locally. Defaults to True.
        """
        try:
            state = {
                "accounts": {address: account.to_dict()
                             for address, account in self.accounts.items()},
                "vaults": {address: vault.address for address, vault in self.vaults.items()},
                "queue": list(self.update_queue.queue),
                "last_saved_block": self.latest_block,
            }

            if local_save:
                with open(self.config.SAVE_STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(state, f)
            else:
                # Save to remote location
                pass

            self.last_saved_block = self.latest_block
        except Exception as ex: # pylint: disable=broad-except
            logger.error("AccountMonitor: Failed to save state: %s", ex, exc_info=True)

    def load_state(self, save_path: str, local_save: bool = True) -> None:
        """
        Load the state of the account monitor from a file.

        Args:
            save_path (str): The path to the saved state file.
            local_save (bool, optional): Whether the state is saved locally. Defaults to True.
        """
        try:
            if local_save and os.path.exists(save_path):
                with open(save_path, "r", encoding="utf-8") as f:
                    state = json.load(f)

                self.vaults = {address: Vault(address, self.config) for address in state["vaults"]}
                self.accounts = {address: Account.from_dict(data, self.vaults, self.config)
                                 for address, data in state["accounts"].items()}

                self.rebuild_queue()

                self.last_saved_block = state["last_saved_block"]
                self.latest_block = self.last_saved_block
                print(f"[STATE] Loaded {len(self.accounts)} accounts, {len(self.vaults)} vaults "
                      f"from saved state (up to block {self.latest_block})")
            elif not local_save:
                # Load from remote location
                pass
            else:
                print("[STATE] No saved state found, starting fresh.")
        except Exception as ex: # pylint: disable=broad-except
            logger.error("AccountMonitor: Failed to load state: %s", ex, exc_info=True)

    def rebuild_queue(self):
        """
        Rebuild queue based on current account health
        """
        self.update_queue = queue.PriorityQueue()
        for address, account in self.accounts.items():
            try:
                account.update_liquidity()

                if account.current_health_score == math.inf:
                    continue

                # Unhealthy accounts get checked immediately, not after liq_time delay
                if account.current_health_score < 1:
                    next_update_time = time.time()
                else:
                    next_update_time = account.time_of_next_update
                self.update_queue.put((next_update_time, address))
            except Exception as ex: # pylint: disable=broad-except
                logger.error("AccountMonitor: Failed to put account %s into rebuilt queue: %s",
                             address, ex, exc_info=True)

        print(f"[MONITOR] Queue rebuilt with {self.update_queue.qsize()} accounts")

    def get_accounts_by_health_score(self):
        """
        Get a list of accounts sorted by health score.

        Returns:
            List[Account]: A list of accounts sorted by health score.
        """
        sorted_accounts = sorted(
            self.accounts.values(),
            key = lambda account: account.current_health_score
        )

        return [(account.address, account.owner, account.subaccount_number,
                 account.current_health_score, account.value_borrowed,
                 account.controller.vault_name, account.controller.vault_symbol)
                 for account in sorted_accounts]

    def periodic_report_low_health_accounts(self):
        """
        Periodically report accounts with low health scores.
        """
        while self.running:
            try:
                sorted_accounts = self.get_accounts_by_health_score()
                post_low_health_account_report(sorted_accounts, self.config)
                time.sleep(self.config.LOW_HEALTH_REPORT_INTERVAL)
            except Exception as ex: # pylint: disable=broad-except
                logger.error("AccountMonitor: Failed to post low health account report: %s", ex,
                              exc_info=True)

    @staticmethod
    def create_from_save_state(chain_id: int, config: ChainConfig, save_path: str, local_save: bool = True) -> "AccountMonitor":
        """
        Create an AccountMonitor instance from a saved state.

        Args:
            save_path (str): The path to the saved state file.
            local_save (bool, optional): Whether the state is saved locally. Defaults to True.

        Returns:
            AccountMonitor: An AccountMonitor instance initialized from the saved state.
        """
        monitor = AccountMonitor(chain_id=chain_id, config=config)
        monitor.load_state(save_path, local_save)
        return monitor

    def periodic_save(self) -> None:
        """
        Periodically save the state of the account monitor.
        Should be run in a standalone thread.
        """
        while self.running:
            time.sleep(self.config.SAVE_INTERVAL)
            self.save_state()

    def stop(self) -> None:
        """
        Stop the account monitor and save its current state.
        """
        self.running = False
        with self.condition:
            self.condition.notify_all()
        self.executor.shutdown(wait=True)
        self.save_state()

class PullOracleHandler:
    """
    Class to handle checking and updating Pull oracles.
    """
    def __init__(self):
        pass

    @staticmethod
    def get_account_values_with_pyth_batch_simulation(vault, account_address, feed_ids, config: ChainConfig):
        update_data = PullOracleHandler.get_pyth_update_data(feed_ids)
        update_fee = PullOracleHandler.get_pyth_update_fee(update_data, config)

        liquidator = config.liquidator

        result = liquidator.functions.simulatePythUpdateAndGetAccountStatus(
            [update_data], update_fee, vault.address, account_address
            ).call({
                "value": update_fee
            })
        return result[0], result[1]

    @staticmethod
    def check_liquidation_with_pyth_batch_simulation(vault, liquidator_address, borrower_address,
                                                     collateral_address, feed_ids, config: ChainConfig):
        update_data = PullOracleHandler.get_pyth_update_data(feed_ids)
        update_fee = PullOracleHandler.get_pyth_update_fee(update_data, config)

        liquidator = config.liquidator

        result = liquidator.functions.simulatePythUpdateAndCheckLiquidation(
            [update_data], update_fee, vault.address,
            liquidator_address, borrower_address, collateral_address
            ).call({
                "value": update_fee
            })
        return result[0], result[1]


    @staticmethod
    def get_feed_ids(vault, config: ChainConfig):
        try:
            oracle_address = vault.oracle_address
            oracle = create_contract_instance(oracle_address, config.ORACLE_ABI_PATH, config)

            unit_of_account = vault.unit_of_account

            collateral_vault_list = vault.get_ltv_list()
            asset_list = []
            for collateral_vault in collateral_vault_list:
                try:
                    asset_list.append(Vault(collateral_vault, config).underlying_asset_address)
                except Exception:  # pylint: disable=broad-except
                    pass  # skip collateral vaults that don't implement EVault interface
            asset_list.append(vault.underlying_asset_address)

            pyth_feed_ids = set()

            for asset in asset_list:
                (_, _, _, configured_oracle_address) = oracle.functions.resolveOracle(0, asset, unit_of_account).call()

                configured_oracle = create_contract_instance(configured_oracle_address,
                                                             config.ORACLE_ABI_PATH, config)

                try:
                    configured_oracle_name = configured_oracle.functions.name().call()
                except Exception as ex: # pylint: disable=broad-except
                    logger.error("PullOracleHandler: Error calling contract for oracle"
                                 " at %s, asset %s: %s", configured_oracle_address, asset, ex)
                    continue
                if configured_oracle_name == "PythOracle":
                    pyth_feed_ids.add(configured_oracle.functions.feedId().call().hex())
                elif configured_oracle_name == "CrossAdapter":
                    pyth_ids = PullOracleHandler.resolve_cross_oracle(
                        configured_oracle, config)
                    pyth_feed_ids.update(pyth_ids)

            return list(pyth_feed_ids)

        except Exception as ex: # pylint: disable=broad-except
            logger.error("PullOracleHandler: Error calling contract: %s", ex, exc_info=True)
            return []

    @staticmethod
    def resolve_cross_oracle(cross_oracle, config):
        pyth_feed_ids = set()

        oracle_base_address = cross_oracle.functions.oracleBaseCross().call()
        oracle_base = create_contract_instance(oracle_base_address, config.ORACLE_ABI_PATH, config)
        oracle_base_name = oracle_base.functions.name().call()

        if oracle_base_name == "PythOracle":
            pyth_feed_ids.add(oracle_base.functions.feedId().call().hex())
        elif oracle_base_name == "CrossAdapter":
            pyth_ids = PullOracleHandler.resolve_cross_oracle(oracle_base, config)
            pyth_feed_ids.update(pyth_ids)

        oracle_quote_address = cross_oracle.functions.oracleCrossQuote().call()
        oracle_quote = create_contract_instance(oracle_quote_address, config.ORACLE_ABI_PATH, config)
        oracle_quote_name = oracle_quote.functions.name().call()

        if oracle_quote_name == "PythOracle":
            pyth_feed_ids.add(oracle_quote.functions.feedId().call().hex())
        elif oracle_quote_name == "CrossAdapter":
            pyth_ids = PullOracleHandler.resolve_cross_oracle(oracle_quote, config)
            pyth_feed_ids.update(pyth_ids)
        return pyth_feed_ids

    @staticmethod
    def get_pyth_update_data(feed_ids):
        pyth_url = "https://hermes.pyth.network/v2/updates/price/latest?"
        for feed_id in feed_ids:
            pyth_url += "ids[]=" + feed_id + "&"
        pyth_url = pyth_url[:-1]

        api_return_data = make_api_request(pyth_url, {}, {})
        return "0x" + api_return_data["binary"]["data"][0]

    @staticmethod
    def get_pyth_update_fee(update_data, config):
        pyth = create_contract_instance(config.PYTH, config.PYTH_ABI_PATH, config)
        return pyth.functions.getUpdateFee([update_data]).call()

class EVCListener:
    """
    Listener class for monitoring EVC events.
    Primarily intended to listen for AccountStatusCheck events.
    Contains handling for processing historical blocks in a batch system on startup.
    """
    def __init__(self, account_monitor: AccountMonitor, config: ChainConfig):
        self.config = config
        self.w3 = config.w3
        self.account_monitor = account_monitor

        self.evc_instance = config.evc

        self.scanned_blocks = set()

    def start_event_monitoring(self) -> None:
        """
        Start monitoring for EVC events.
        Scans from last scanned block stored by account monitor
        up to the current block number (minus 1 to try to account for reorgs).
        """
        while True:
            try:
                current_block = self.w3.eth.block_number - 1

                if self.account_monitor.latest_block < current_block:
                    self.scan_block_range_for_account_status_check(
                        self.account_monitor.latest_block,
                        current_block)
            except Exception as ex: # pylint: disable=broad-except
                logger.error("EVCListener: Unexpected exception in event monitoring: %s",
                             ex, exc_info=True)

            time.sleep(self.config.SCAN_INTERVAL)

    #pylint: disable=W0102
    def scan_block_range_for_account_status_check(self,
                                                  start_block: int,
                                                  end_block: int,
                                                  max_retries: int = 3,
                                                  seen_accounts: set = set()) -> None:
        """
        Scan a range of blocks for AccountStatusCheck events.

        Args:
            start_block (int): The starting block number.
            end_block (int): The ending block number.
            max_retries (int, optional): Maximum number of retry attempts. 
                                        Defaults to config.NUM_RETRIES.
            
        """
        for attempt in range(max_retries):
            try:
                logs = self.evc_instance.events.AccountStatusCheck().get_logs(
                    from_block=start_block,
                    to_block=end_block)

                for log in logs:
                    vault_address = log["args"]["controller"]
                    account_address = log["args"]["account"]

                    # if we've seen the account already and the status
                    # check is not due to changing controller
                    if account_address in seen_accounts:
                        acct = self.account_monitor.accounts.get(account_address)
                        if acct is None:
                            continue

                        same_controller = acct.controller.address == Web3.to_checksum_address(
                            vault_address)

                        if same_controller:
                            continue
                    else:
                        # remember that we've processed this account (regardless of
                        # whether it successfully made it into accounts dict)
                        seen_accounts.add(account_address)

                    try:
                        self.account_monitor.update_account_on_status_check_event(
                            account_address,
                            vault_address)
                    except Exception as ex: # pylint: disable=broad-except
                        logger.error("EVCListener: Exception updating account %s "
                                     "on AccountStatusCheck event: %s",
                                     account_address, ex, exc_info=True)

                self.account_monitor.latest_block = end_block
                return
            except Exception as ex: # pylint: disable=broad-except
                logger.error("EVCListener: Exception scanning block range %s to %s "
                             "(attempt %s/%s): %s",
                             start_block, end_block, attempt + 1, max_retries, ex, exc_info=True)
                if attempt == max_retries - 1:
                    logger.error("EVCListener: "
                                 "Failed to scan block range %s to %s after %s attempts",
                                 start_block, end_block, max_retries, exc_info=True)
                else:
                    time.sleep(self.config.RETRY_DELAY) # cooldown between retries


    def batch_account_logs_on_startup(self) -> None:
        """
        Batch process account logs on startup.
        Goes in reverse order to build smallest queue possible with most up to date info
        """
        try:
            # If the account monitor has a saved state,
            # assume it has been loaded from that and start from the last saved block
            start_block = max(int(self.config.EVC_DEPLOYMENT_BLOCK),
                              self.account_monitor.last_saved_block)

            current_block = self.w3.eth.block_number

            batch_block_size = self.config.BATCH_SIZE

            print(f"[STARTUP] Scanning EVC history: blocks {start_block} → {current_block} "
                  f"(batch size: {batch_block_size})")

            seen_accounts = set()

            while start_block < current_block:
                end_block = min(start_block + batch_block_size, current_block)

                self.scan_block_range_for_account_status_check(start_block, end_block,
                                                               seen_accounts=seen_accounts)
                self.account_monitor.save_state()

                start_block = end_block + 1

                time.sleep(self.config.BATCH_INTERVAL) # Sleep in between batches to avoid rate limiting

            print(f"[STARTUP] Historical scan complete. "
                  f"{len(seen_accounts)} unique accounts discovered.")

        except Exception as ex: # pylint: disable=broad-except
            logger.error("EVCListener: "
                         "Unexpected exception in batch scanning account logs on startup: %s",
                         ex, exc_info=True)

    @staticmethod
    def get_account_owner_and_subaccount_number(account, config):
        evc = config.evc
        owner = evc.functions.getAccountOwner(account).call()
        if owner == "0x0000000000000000000000000000000000000000":
            owner = account

        subaccount_number = int(int(account, 16) ^ int(owner, 16))
        return owner, subaccount_number

# Future feature, smart monitor to trigger manual update of an account
# based on a large price change (or some other trigger)
class SmartUpdateListener:
    """
    Boiler plate listener class.
    Intended to be implemented with some other trigger condition to update accounts.
    """
    def __init__(self, account_monitor: AccountMonitor):
        self.account_monitor = account_monitor

    def trigger_manual_update(self, account: Account) -> None:
        """
        Boilerplate to trigger a manual update for a specific account.

        Args:
            account (Account): The account to update.
        """
        self.account_monitor.update_account_liquidity(account)

liquidation_error_slack_cooldown = {}

class Liquidator:
    """
    Class to handle liquidation logic for accounts
    This class provides static methods for simulating liquidations, calculating
    liquidation profits, and executing liquidation transactions. 
    """
    def __init__(self):
        pass

    @staticmethod
    def simulate_liquidation(vault: Vault,
                             violator_address: str,
                             violator_account: Account, config: ChainConfig) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Simulate the liquidation of an account.
        Chooses the maximum profitable liquidation from the available collaterals, if one exists.

        Args:
            vault (Vault): The vault associated with the account.
            violator_address (str): The address of the account to potentially liquidate.

        Returns:
            Tuple[bool, Optional[Dict[str, Any]]]: A tuple containing a boolean indicating
            if liquidation is profitable, and a dictionary with liquidation details 
            & transaction object if profitable.
        """

        evc_instance = config.evc
        collateral_list = evc_instance.functions.getCollaterals(violator_address).call()
        borrowed_asset = vault.underlying_asset_address
        liquidator_contract = config.liquidator

        print(f"[LIQ] EVC collaterals for {violator_address}: {len(collateral_list)} found")
        if not collateral_list:
            print(f"[LIQ]   → no collateral enabled — cannot liquidate")
            return (False, None, None)

        max_profit_data = {
            "tx": None,
            "profit": 0,
            "collateral_address": None,
            "collateral_asset": None,
            "leftover_borrow": 0,
            "leftover_borrow_in_eth": 0
        }
        max_profit_params = None

        collateral_vaults = {}
        for collateral in collateral_list:
            try:
                collateral_vaults[collateral] = Vault(collateral, config)
            except Exception:  # pylint: disable=broad-except
                # Not an EVault — print what we can via plain ERC20 + oracle
                try:
                    erc20 = create_contract_instance(collateral, config.ERC20_ABI_PATH, config)
                    dec = erc20.functions.decimals().call()
                    sym = erc20.functions.symbol().call()
                    bal = erc20.functions.balanceOf(Web3.to_checksum_address(violator_address)).call()
                    one_tok = 10 ** dec
                    try:
                        lltv_raw = vault.ltv_liquidation(collateral)
                        lltv_str = f"{lltv_raw / 100:.4f}%"
                    except Exception:  # pylint: disable=broad-except
                        lltv_str = "N/A"
                    try:
                        oracle = create_contract_instance(vault.oracle_address, config.ORACLE_ABI_PATH, config)
                        price_raw = oracle.functions.getQuote(
                            one_tok,
                            Web3.to_checksum_address(collateral),
                            Web3.to_checksum_address(vault.unit_of_account)
                        ).call()
                        price_str = _fmt_uoa(price_raw, vault.unit_of_account, config)
                    except Exception:  # pylint: disable=broad-except
                        price_str = "N/A"
                    print(f"[LIQ]   Collateral (ERC20): {sym} ({collateral})")
                    print(f"[LIQ]   LLTV             : {lltv_str}")
                    print(f"[LIQ]   Balance          : {bal / one_tok:.8f} {sym}")
                    print(f"[LIQ]   Oracle price     : {price_str} per token")
                    print(f"[LIQ]   → non-EVault collateral — calling checkLiquidation on debt vault anyway")
                    try:
                        (max_repay_erc20, seized_erc20) = vault.check_liquidation(
                            violator_address, collateral, config.LIQUIDATOR_EOA)
                        if max_repay_erc20 == 0 or seized_erc20 == 0:
                            print(f"[LIQ]   → checkLiquidation: max_repay={max_repay_erc20}, "
                                  f"seized={seized_erc20} → not liquidatable")
                        else:
                            print(f"[LIQ]   → checkLiquidation: max_repay={max_repay_erc20} | "
                                  f"seized={seized_erc20} → LIQUIDATABLE")
                            MAX_UINT256 = 2**256 - 1
                            ZERO_ERC20 = "0x0000000000000000000000000000000000000000"
                            evc_erc20 = config.evc
                            enable_ctrl_erc20 = evc_erc20.encode_abi(
                                "enableController",
                                [config.LIQUIDATOR_EOA, Web3.to_checksum_address(vault.address)]
                            )
                            enable_col_erc20 = evc_erc20.encode_abi(
                                "enableCollateral",
                                [config.LIQUIDATOR_EOA, Web3.to_checksum_address(collateral)]
                            )
                            liquidate_data_erc20 = vault.instance.encode_abi(
                                "liquidate",
                                [Web3.to_checksum_address(violator_address),
                                 Web3.to_checksum_address(collateral),
                                 MAX_UINT256,
                                 0]
                            )
                            batch_items_erc20 = [
                                (config.EVC,                                     ZERO_ERC20,            0, enable_ctrl_erc20),
                                (config.EVC,                                     ZERO_ERC20,            0, enable_col_erc20),
                                (Web3.to_checksum_address(vault.address), config.LIQUIDATOR_EOA, 0, liquidate_data_erc20),
                            ]
                            suggested_gas_price_erc20 = int(config.w3.eth.gas_price * 1.2)
                            nonce_erc20 = config.w3.eth.get_transaction_count(config.LIQUIDATOR_EOA)
                            try:
                                estimated_gas_erc20 = evc_erc20.functions.batch(batch_items_erc20).estimate_gas({
                                    "from": config.LIQUIDATOR_EOA,
                                    "nonce": nonce_erc20,
                                })
                                gas_limit_erc20 = int(estimated_gas_erc20 * 1.3)
                                print(f"[LIQ]   → dry-run OK (ERC20) | estimated gas={estimated_gas_erc20} | using gas_limit={gas_limit_erc20}")
                            except Exception as eg_err:  # pylint: disable=broad-except
                                print(f"[LIQ]   → dry-run FAILED (ERC20) — tx would revert, skipping. Reason: {eg_err}")
                                continue
                            liquidation_tx_erc20 = evc_erc20.functions.batch(batch_items_erc20).build_transaction({
                                "chainId":  config.CHAIN_ID,
                                "from":     config.LIQUIDATOR_EOA,
                                "nonce":    nonce_erc20,
                                "gasPrice": suggested_gas_price_erc20,
                                "gas":      gas_limit_erc20,
                            })
                            profit_data_erc20 = {
                                "tx": liquidation_tx_erc20,
                                "profit": 1,
                                "collateral_address": collateral,
                                "collateral_vault_symbol": sym,
                                "collateral_asset": collateral,
                                "leftover_borrow": max_repay_erc20,
                                "leftover_borrow_in_eth": 0,
                                "seized_shares": seized_erc20,
                                "underlying_collateral_received": seized_erc20,
                                "vault_instance": vault.instance,
                            }
                            if profit_data_erc20["profit"] > max_profit_data["profit"]:
                                max_profit_data = profit_data_erc20
                                max_profit_params = (
                                    violator_address, vault.address, borrowed_asset,
                                    collateral, collateral, max_repay_erc20,
                                    seized_erc20, config.PROFIT_RECEIVER
                                )
                    except Exception as ex_chk:  # pylint: disable=broad-except
                        print(f"[LIQ]   → checkLiquidation call failed: {ex_chk}")
                except Exception as ex:  # pylint: disable=broad-except
                    print(f"[LIQ]   → {collateral}: unrecognised collateral ({ex})")

        for collateral, collateral_vault in collateral_vaults.items():
            try:
                # Fetch LLTV for this collateral from the debt vault
                try:
                    lltv_raw = vault.ltv_liquidation(collateral)
                    lltv_str = f"{lltv_raw / 100:.4f}%"
                except Exception:  # pylint: disable=broad-except
                    lltv_str = "N/A"

                # Fetch violator's balance in this collateral vault + oracle price
                try:
                    erc20 = create_contract_instance(
                        collateral_vault.underlying_asset_address, config.ERC20_ABI_PATH, config)
                    decimals = erc20.functions.decimals().call()
                    one_token = 10 ** decimals
                    shares_balance = collateral_vault.instance.functions.balanceOf(
                        Web3.to_checksum_address(violator_address)).call()
                    underlying_balance = collateral_vault.convert_to_assets(shares_balance) if shares_balance > 0 else 0
                    token_amount_str = f"{underlying_balance / one_token:.8f} {collateral_vault.vault_symbol}"
                except Exception:  # pylint: disable=broad-except
                    token_amount_str = "N/A"

                try:
                    oracle = create_contract_instance(vault.oracle_address, config.ORACLE_ABI_PATH, config)
                    oracle_price_raw = oracle.functions.getQuote(
                        one_token,
                        Web3.to_checksum_address(collateral_vault.underlying_asset_address),
                        Web3.to_checksum_address(vault.unit_of_account)
                    ).call()
                    oracle_price_str = _fmt_uoa(oracle_price_raw, vault.unit_of_account, config)
                except Exception:  # pylint: disable=broad-except
                    oracle_price_str = "N/A"

                print(f"[LIQ]   Collateral vault : {collateral_vault.vault_symbol} ({collateral})")
                print(f"[LIQ]   LLTV             : {lltv_str}")
                print(f"[LIQ]   Balance          : {token_amount_str}")
                print(f"[LIQ]   Oracle price     : {oracle_price_str} per token")

                liquidation_results = Liquidator.calculate_liquidation_profit(vault,
                                                                      violator_address,
                                                                      borrowed_asset,
                                                                      collateral_vault,
                                                                      liquidator_contract,
                                                                      config)
                profit_data, params = liquidation_results

                if profit_data["profit"] > max_profit_data["profit"]:
                    max_profit_data = profit_data
                    max_profit_params = params
            except Exception as ex: # pylint: disable=broad-except
                logger.error("Liquidator: Exception simulating liquidation "
                             "for account %s with collateral %s: %s",
                             violator_address, collateral, ex, exc_info=True)
                print(f"[LIQ]   → exception for collateral {collateral}: {ex}")
                continue


        if max_profit_data["tx"]:
            return (True, max_profit_data, max_profit_params)
        return (False, None, None)

    @staticmethod
    def calculate_liquidation_profit(vault: Vault,
                                     violator_address: str,
                                     borrowed_asset: str,
                                     collateral_vault: Vault,
                                     _unused: Any,
                                     config: ChainConfig) -> Tuple[Dict[str, Any], Any]:
        """
        Check whether a liquidation is available and build a direct vault.liquidate() transaction.

        The EOA calls liquidate() directly on the debt vault (vault must already have
        controller/collateral enabled, or the vault handles it internally).
        repayAssets = MAX_UINT256, minYieldBalance = 0.
        No swap or external API is required — the vault's liquidation math guarantees
        seized_value > repay_value whenever max_repay > 0.

        Args:
            vault (Vault): The vault that violator has borrowed from.
            violator_address (str): The address of the account to liquidate.
            borrowed_asset (str): The address of the borrowed asset.
            collateral_vault (Vault): The collateral vault to seize.
            _unused: Unused (was liquidator_contract in the original implementation).

        Returns:
            Tuple[Dict, Any]: Liquidation data dict and params tuple, or ({"profit": 0}, None).
        """
        collateral_vault_address = collateral_vault.address
        collateral_asset = collateral_vault.underlying_asset_address

        (max_repay, seized_collateral_shares) = vault.check_liquidation(violator_address,
                                                                        collateral_vault_address,
                                                                        config.LIQUIDATOR_EOA)

        if max_repay == 0 or seized_collateral_shares == 0:
            print(f"[LIQ]   → checkLiquidation: max_repay={max_repay}, seized={seized_collateral_shares} "
                  f"→ not liquidatable (bad debt or cooldown)")
            return ({"profit": 0}, None)

        underlying_collateral_received = collateral_vault.convert_to_assets(seized_collateral_shares)
        print(f"[LIQ]   → checkLiquidation: max_repay={max_repay} | seized_shares={seized_collateral_shares} "
              f"| underlying_received={underlying_collateral_received} → LIQUIDATABLE")

        # EVC batch: enableController → enableCollateral → liquidate
        # Must pre-register our EOA as controller+collateral holder before seizing.
        MAX_UINT256 = 2**256 - 1
        ZERO = "0x0000000000000000000000000000000000000000"
        evc = config.evc
        enable_ctrl = evc.encode_abi(
            "enableController",
            [config.LIQUIDATOR_EOA, Web3.to_checksum_address(vault.address)]
        )
        enable_col = evc.encode_abi(
            "enableCollateral",
            [config.LIQUIDATOR_EOA, Web3.to_checksum_address(collateral_vault_address)]
        )
        liquidate_data = vault.instance.encode_abi(
            "liquidate",
            [Web3.to_checksum_address(violator_address),
             Web3.to_checksum_address(collateral_vault_address),
             MAX_UINT256,
             0]
        )
        batch_items = [
            (config.EVC,                                       ZERO,                  0, enable_ctrl),
            (config.EVC,                                       ZERO,                  0, enable_col),
            (Web3.to_checksum_address(vault.address), config.LIQUIDATOR_EOA, 0, liquidate_data),
        ]
        suggested_gas_price = int(config.w3.eth.gas_price * 1.2)
        nonce = config.w3.eth.get_transaction_count(config.LIQUIDATOR_EOA)
        try:
            estimated_gas = evc.functions.batch(batch_items).estimate_gas({
                "from": config.LIQUIDATOR_EOA,
                "nonce": nonce,
            })
            gas_limit = int(estimated_gas * 1.3)
            print(f"[LIQ]   → dry-run OK | estimated gas={estimated_gas} | using gas_limit={gas_limit}")
        except Exception as e:  # pylint: disable=broad-except
            print(f"[LIQ]   → dry-run FAILED — tx would revert, aborting to save gas. Reason: {e}")
            return ({"profit": 0}, None)
        liquidation_tx = evc.functions.batch(batch_items).build_transaction({
            "chainId":  config.CHAIN_ID,
            "from":     config.LIQUIDATOR_EOA,
            "nonce":    nonce,
            "gasPrice": suggested_gas_price,
            "gas":      gas_limit,
        })

        params = (
            violator_address,
            vault.address,
            borrowed_asset,
            collateral_vault_address,
            collateral_asset,
            max_repay,
            seized_collateral_shares,
            config.PROFIT_RECEIVER
        )

        return ({
            "tx": liquidation_tx,
            "profit": 1,  # sentinel — vault math guarantees seized_value > repay_value
            "collateral_address": collateral_vault_address,
            "collateral_vault_symbol": collateral_vault.vault_symbol,
            "collateral_asset": collateral_asset,
            "leftover_borrow": max_repay,
            "leftover_borrow_in_eth": 0,
            "seized_shares": seized_collateral_shares,
            "underlying_collateral_received": underlying_collateral_received,
            "vault_instance": vault.instance,
        }, params)

    @staticmethod
    def execute_liquidation(liquidation_transaction: Dict[str, Any], config: ChainConfig,
                            debt_vault_instance=None) -> None:
        """
        Execute a liquidation transaction.

        Args:
            liquidation_transaction (Dict[str, Any]): The liquidation transaction details.
            debt_vault_instance: Optional vault contract instance to parse Liquidate events from.
        """
        try:
            signed_tx = config.w3.eth.account.sign_transaction(liquidation_transaction,
                                                        config.LIQUIDATOR_EOA_PRIVATE_KEY)
            tx_hash = config.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_receipt = config.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            status = "SUCCESS" if tx_receipt.status == 1 else "REVERTED"
            print(f"[LIQ] Tx {status} | hash={tx_hash.hex()} | block={tx_receipt.blockNumber} | gas={tx_receipt.gasUsed}")

            if debt_vault_instance:
                try:
                    events = debt_vault_instance.events.Liquidate().process_receipt(
                        tx_receipt, errors=DISCARD)
                    for ev in events:
                        a = ev['args']
                        print(f"[LIQ] Liquidate event: liquidator={a.get('liquidator')} | "
                              f"violator={a.get('violator')} | "
                              f"collateral={a.get('collateral')} | "
                              f"repaidAssets={a.get('repaidAssets')} | "
                              f"yieldBalance={a.get('yieldBalance')}")
                except Exception:  # pylint: disable=broad-except
                    pass

            return tx_hash.hex(), tx_receipt
        except Exception as ex: # pylint: disable=broad-except
            logger.error("Unexpected error in executing liquidation: %s", ex, exc_info=True)
            print(f"[ERROR] Liquidation execution failed: {ex}")
            return None, None

class Quoter:
    """
    Provides access to 1inch quotes and swap data generation functions
    """
    def __init__(self):
        pass
    
    @staticmethod
    def get_swap_api_quote(
        chain_id: int,
        token_in: str,
        token_out: str,
        amount: int, # exact in - amount to sell, exact out - amount to buy, exact out repay - estimated amount to buy (from current debt)
        min_amount_out: int,
        receiver: str, # vault to swap or repay to
        vault_in: str,
        account_in: str,
        account_out: str,
        swapper_mode: str,
        slippage: float, #in percent 1 = 1%
        deadline: int,
        is_repay: bool,
        current_debt: int, # needed in exact input or output and with `isRepay` set
        target_debt: int, # ignored if not in target debt mode
        skip_sweep_deposit_out: bool, # this flag will leve the sold assets in the Swapper contract
        config
    ):

        params = {
            "chainId": str(chain_id),
            "tokenIn": token_in,
            "tokenOut": token_out, 
            "amount": str(amount),
            "receiver": receiver,
            "vaultIn": vault_in,
            "origin": config.LIQUIDATOR_EOA,
            "accountIn": account_in,
            "accountOut": account_out,
            "swapperMode": swapper_mode,  # TARGET_DEBT mode
            "slippage": str(slippage),
            "deadline": str(deadline), 
            "isRepay": str(is_repay),
            "currentDebt": str(current_debt),
            "targetDebt": str(target_debt),
            "skipSweepDepositOut": str(skip_sweep_deposit_out),
        }

        response = make_api_request(config.SWAP_API_URL, headers={}, params=params)

        if not response or not response["success"]:
            logger.error("Unable to get quote from swap api")
            return None

        amount_out = int(response["data"]["amountOut"])

        if amount_out < min_amount_out:
            logger.error("Quote too low")
            return None

        return response["data"]

if __name__ == "__main__":
    try:
        pass

    except Exception as e: # pylint: disable=broad-except
        logger.critical("Uncaught exception: %s", e, exc_info=True)
