"""
Polymarket CTF (Conditional Token Framework) operations.

On-chain operations for managing outcome tokens:
  - MERGE:  1 Up + 1 Down → $1 USDC (lock in pair profit mid-market)
  - REDEEM: After resolution, winning tokens → $1 USDC each
  - SPLIT:  $1 USDC → 1 Up + 1 Down (mint new tokens)

These interact directly with the CTF smart contract on Polygon,
NOT through the CLOB API (which only handles order placement).

Contract: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045 (Polygon)
Collateral: USDC.e 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

import asyncio
from typing import Optional
from src.monitoring.logger import get_logger

log = get_logger("ctf_ops")

# Polygon contract addresses
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Minimal ABIs for the CTF operations we need
CTF_ABI = [
    {
        "name": "mergePositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "splitPosition",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# ERC20 approve ABI
ERC20_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# Standard binary partition: [1, 2] = [Up, Down]
BINARY_PARTITION = [1, 2]
# Zero parent collection for top-level positions
PARENT_COLLECTION_ID = b'\x00' * 32


class CTFOperations:
    """
    On-chain CTF operations for Polymarket.
    
    Requires:
      - web3.py installed
      - Private key with POL for gas on Polygon
      - USDC.e balance for split operations
      - Token balances for merge/redeem operations
    """

    def __init__(self, private_key: str,
                 rpc_url: str = "https://polygon-rpc.com",
                 dry_run: bool = True):
        """
        Args:
            private_key: Ethereum private key (0x-prefixed).
            rpc_url: Polygon RPC endpoint.
            dry_run: If True, simulate without sending transactions.
        """
        self._private_key = private_key
        self._rpc_url = rpc_url
        self._dry_run = dry_run
        self._w3 = None
        self._ctf = None
        self._usdc = None
        self._account = None
        self._initialized = False

    async def initialize(self):
        """Initialize web3 connection and contract instances."""
        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            if not self._w3.is_connected():
                log.error("web3_not_connected", rpc=self._rpc_url)
                return False

            self._account = self._w3.eth.account.from_key(self._private_key)
            self._ctf = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_CONTRACT),
                abi=CTF_ABI,
            )
            self._usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=ERC20_APPROVE_ABI,
            )
            self._initialized = True

            log.info("ctf_initialized",
                     address=self._account.address,
                     dry_run=self._dry_run)
            return True

        except ImportError:
            log.error("web3_not_installed",
                      msg="Install with: pip install web3")
            return False
        except Exception as e:
            log.error("ctf_init_error", error=str(e))
            return False

    async def merge_positions(self, condition_id: str,
                               amount: int) -> Optional[str]:
        """
        Merge matched pairs: 1 Up + 1 Down → $1 USDC.
        
        This is the KEY profit-taking operation for a pair-matching MM.
        Call this when you have matched pairs to lock in guaranteed profit.
        
        Args:
            condition_id: The market's condition ID (bytes32 hex).
            amount: Number of pairs to merge (in token units, typically 10^6).
            
        Returns:
            Transaction hash if successful, None otherwise.
        """
        if not self._initialized:
            log.error("ctf_not_initialized")
            return None

        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        if self._dry_run:
            log.info("dry_merge", condition=condition_id[:10],
                     pairs=amount, usdc_out=f"${amount / 1e6:.2f}")
            return f"DRY-MERGE-{condition_id[:8]}"

        try:
            tx = self._ctf.functions.mergePositions(
                self._w3.to_checksum_address(USDC_ADDRESS),
                PARENT_COLLECTION_ID,
                condition_bytes,
                BINARY_PARTITION,
                amount,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 200_000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                log.info("merge_success",
                         tx_hash=tx_hash.hex()[:16],
                         pairs=amount,
                         usdc_out=f"${amount / 1e6:.2f}")
                return tx_hash.hex()
            else:
                log.error("merge_reverted", tx_hash=tx_hash.hex()[:16])
                return None

        except Exception as e:
            log.error("merge_error", error=str(e))
            return None

    async def redeem_positions(self, condition_id: str) -> Optional[str]:
        """
        Redeem winning tokens after market resolution.
        
        Call this after a market has resolved. Winning tokens are
        burned and USDC is returned.
        
        Args:
            condition_id: The resolved market's condition ID.
            
        Returns:
            Transaction hash if successful, None otherwise.
        """
        if not self._initialized:
            log.error("ctf_not_initialized")
            return None

        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        # Check if market is actually resolved
        try:
            payout_denom = self._ctf.functions.payoutDenominator(
                condition_bytes
            ).call()
            if payout_denom == 0:
                log.warning("market_not_resolved", condition=condition_id[:10])
                return None
        except Exception:
            pass

        if self._dry_run:
            log.info("dry_redeem", condition=condition_id[:10])
            return f"DRY-REDEEM-{condition_id[:8]}"

        try:
            tx = self._ctf.functions.redeemPositions(
                self._w3.to_checksum_address(USDC_ADDRESS),
                PARENT_COLLECTION_ID,
                condition_bytes,
                BINARY_PARTITION,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 200_000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                log.info("redeem_success", tx_hash=tx_hash.hex()[:16],
                         condition=condition_id[:10])
                return tx_hash.hex()
            else:
                log.error("redeem_reverted", tx_hash=tx_hash.hex()[:16])
                return None

        except Exception as e:
            log.error("redeem_error", error=str(e))
            return None

    async def split_position(self, condition_id: str,
                              amount: int) -> Optional[str]:
        """
        Split USDC into Up + Down tokens.
        
        $1 USDC → 1 Up token + 1 Down token.
        Useful for providing initial liquidity or minting tokens to sell.
        
        Note: Requires prior USDC approval to CTF contract.
        
        Args:
            condition_id: The market's condition ID.
            amount: USDC amount in base units (10^6 = $1).
            
        Returns:
            Transaction hash if successful, None otherwise.
        """
        if not self._initialized:
            log.error("ctf_not_initialized")
            return None

        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        if self._dry_run:
            log.info("dry_split", condition=condition_id[:10],
                     usdc_in=f"${amount / 1e6:.2f}")
            return f"DRY-SPLIT-{condition_id[:8]}"

        try:
            # Check and set USDC approval if needed
            await self._ensure_usdc_approval(amount)

            tx = self._ctf.functions.splitPosition(
                self._w3.to_checksum_address(USDC_ADDRESS),
                PARENT_COLLECTION_ID,
                condition_bytes,
                BINARY_PARTITION,
                amount,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 250_000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                log.info("split_success", tx_hash=tx_hash.hex()[:16],
                         usdc_in=f"${amount / 1e6:.2f}")
                return tx_hash.hex()
            else:
                log.error("split_reverted", tx_hash=tx_hash.hex()[:16])
                return None

        except Exception as e:
            log.error("split_error", error=str(e))
            return None

    async def get_token_balance(self, token_id: int) -> int:
        """Get balance of a specific outcome token."""
        if not self._initialized:
            return 0
        try:
            balance = self._ctf.functions.balanceOf(
                self._account.address, token_id
            ).call()
            return balance
        except Exception as e:
            log.error("balance_error", error=str(e))
            return 0

    async def is_market_resolved(self, condition_id: str) -> bool:
        """Check if a market has been resolved."""
        if not self._initialized:
            return False
        try:
            condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            payout_denom = self._ctf.functions.payoutDenominator(
                condition_bytes
            ).call()
            return payout_denom > 0
        except Exception:
            return False

    async def _ensure_usdc_approval(self, amount: int):
        """Ensure CTF contract has USDC approval."""
        try:
            allowance = self._usdc.functions.allowance(
                self._account.address,
                self._w3.to_checksum_address(CTF_CONTRACT),
            ).call()

            if allowance < amount:
                # Approve max uint256
                max_approval = 2**256 - 1
                tx = self._usdc.functions.approve(
                    self._w3.to_checksum_address(CTF_CONTRACT),
                    max_approval,
                ).build_transaction({
                    "from": self._account.address,
                    "nonce": self._w3.eth.get_transaction_count(
                        self._account.address
                    ),
                    "gas": 60_000,
                    "gasPrice": self._w3.eth.gas_price,
                })
                signed = self._account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(
                    signed.raw_transaction
                )
                self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                log.info("usdc_approved", tx_hash=tx_hash.hex()[:16])

        except Exception as e:
            log.error("approval_error", error=str(e))
            raise
