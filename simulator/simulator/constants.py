"""Simulator-local generation and runtime tuning values."""

AMOUNT_LOG_NORMAL_MU = 8.5
AMOUNT_LOG_NORMAL_SIGMA = 1.2
AMOUNT_MIN_INR = 50
AMOUNT_MAX_INR = 50_000
DEFAULT_SEED = 42
REQUIRED_INVOICE_FIELDS = ["vendor_name", "amount", "category", "invoice_date", "submitted_by"]
BLOCKED_VENDORS = frozenset({
    "ShellCo Industries", "FastCash Consulting", "QuickBill Ltd",
    "NoName Supplies", "Generic Vendor",
})
DEGRADED_MISSING_FIELD_PROB = 0.18
DEGRADED_BOUNDARY_FRACTION = 0.05
DEGRADED_AMBIGUOUS_VENDOR_PROB = 0.25
RECOVERY_MISSING_FIELD_PROB = 0.05
RECOVERY_BOUNDARY_FRACTION = 0.10
RECOVERY_AMBIGUOUS_VENDOR_PROB = 0.10

# ScriptedAgent mistake rate per demo phase. "good" and "recovery" run a
# mostly-reliable agent; "degraded" injects a real performance drop that the
# trust engine detects as drift and claws back.
PHASE_ERROR_RATES = {
    "good": 0.05,
    "degraded": 0.30,
    "recovery": 0.10,
}

WILSON_Z = 1.96
DEFAULT_API_BASE_URL = "http://localhost:8000"
DEFAULT_API_VERSION = "v1"
AGENT_PROMPT_VERSION = "v1"
CACHE_DIR = "fixtures/cache"
