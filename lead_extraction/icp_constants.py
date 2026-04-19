"""
ICP constants for Web3 founders (titles, geo, headcount, keywords).
Override via env where noted in config (optional JSON arrays).
"""

# Apollo `person_titles` — cast a wide net; strict founder filter is post-processing.
DEFAULT_APOLLO_PERSON_TITLES: list[str] = [
    "Founder",
    "Co-Founder",
    "CEO",
    "Chief Executive Officer",
    "Managing Director",
    "President",
]

# Primary + secondary headcount per ICP (Apollo uses "min,max" strings).
DEFAULT_ORGANIZATION_NUM_EMPLOYEES_RANGES: list[str] = ["1,10", "11,20"]

DEFAULT_PERSON_LOCATIONS: list[str] = [
    "United States",
    "United Kingdom",
    "Canada",
    "Germany",
    "Singapore",
    "United Arab Emirates",
    "India",
    "Remote",
]

# Post-filter: Web3 / crypto signals (lowercase matching).
WEB3_KEYWORDS: tuple[str, ...] = (
    "web3",
    "crypto",
    "blockchain",
    "defi",
    "nft",
    "nfts",
    "dao",
    "token",
    "tokens",
    "ethereum",
    "eth ",
    "bitcoin",
    "solana",
    "polygon",
    "arbitrum",
    "optimism",
    "base ",
    "layer 2",
    "l2",
    "rollup",
    "smart contract",
    "dapp",
    "dapps",
    "decentralized",
    "on-chain",
    "onchain",
    "wallet",
    "staking",
    "validator",
    "protocol",
    "dex",
    "cex",
    "zero knowledge",
    "zk-",
    "zk ",
    "metaverse",
    "gamefi",
    "rwa",
    "real world asset",
)

# Strict founder-level titles (substring match on normalized title).
FOUNDER_TITLE_SUBSTRINGS: tuple[str, ...] = (
    "founder",
    "co-founder",
    "cofounder",
    "ceo",
    "chief executive",
    "managing director",
    "president",
)

# Headcount buckets we accept after Apollo response normalization.
PRIMARY_EMPLOYEE_MAX = 10
SECONDARY_EMPLOYEE_MAX = 20
