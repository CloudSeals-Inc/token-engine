"""
MIBA — Token Issuance Engine
Service: token-engine
Formula: MIBA_TOKENS = base_rate × weight_kg × severity_multiplier × verification_multiplier × carbon_bonus
Ledger: Postgres (off-chain Phase 1) → Polygon ERC-1155 (Phase 2)
"""

import os
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from .database import db, connect_to_mongo, close_mongo_connection


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="MIBA Token Engine",
    description="WasteKI token issuance — waste verified → MIBA tokens minted",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*", "https://miba-ui-340635219170.europe-west1.run.app"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup_db_client():
    await connect_to_mongo()

@app.on_event("shutdown")
async def shutdown_db_client():
    await close_mongo_connection()


# ─── Token config ──────────────────────────────────────────────────────────
TOKEN_MODE = os.getenv("TOKEN_MODE", "OFFCHAIN")  # OFFCHAIN | POLYGON
CARBON_ENGINE_URL = os.getenv("CARBON_ENGINE_URL", "http://carbon-engine:8080")

# INR conversion: 1 MIBA token = X INR (updated weekly from scrap market)
TOKEN_INR_RATES = {
    "W01": 0.5,   "W02": 8.0,  "W03": 5.0,  "W04": 12.0, "W05": 50.0,
    "W06": 3.0,   "W07": 1.5,  "W08": 20.0, "W09": 0.0,  "W10": 4.0,
    "W11": 5.0,   "W12": 0.8,  "W13": 0.0,  "W14": 1.0,  "W15": 0.2, "W16": 1.5,
}

# Base token rate per kg (before multipliers)
BASE_TOKEN_RATES = {
    "W01": 0.8,  "W02": 2.2,  "W03": 1.8,  "W04": 2.8,  "W05": 5.0,
    "W06": 1.2,  "W07": 0.6,  "W08": 4.5,  "W09": 0.0,  "W10": 1.4,
    "W11": 1.6,  "W12": 0.3,  "W13": 0.0,  "W14": 0.5,  "W15": 0.1, "W16": 0.4,
}

SEVERITY_SCORES = {
    "W01": 5,  "W02": 8,  "W03": 9,  "W04": 6,  "W05": 7,
    "W06": 4,  "W07": 3,  "W08": 10, "W09": 10, "W10": 6,
    "W11": 7,  "W12": 3,  "W13": 10, "W14": 8,  "W15": 2,  "W16": 5,
}

# Revenue split
SPLIT_COLLECTOR = float(os.getenv("SPLIT_COLLECTOR", "0.60"))
SPLIT_PLATFORM  = float(os.getenv("SPLIT_PLATFORM",  "0.25"))
SPLIT_MUNICIPAL = float(os.getenv("SPLIT_MUNICIPAL", "0.15"))


# ─── Database: MongoDB (replacing in-memory Phase 1) ───────────────────────
# Collections: ledger



# ─── Schemas ───────────────────────────────────────────────────────────────

class TokenIssuanceRequest(BaseModel):
    work_order_id: str
    collector_id: str
    category_code: str = Field(..., pattern=r"^W(0[1-9]|1[0-6])$")
    weight_kg: float = Field(..., gt=0)
    co2e_avoided_kg: float = Field(..., ge=0)
    supervisor_verified: bool
    verification_score: float = Field(default=1.0, ge=0, le=1.0)

class BulkTokenRequest(BaseModel):
    work_order_id: str
    collector_id: str
    verified: bool
    verification_score: float = 1.0
    waste_items: list[dict]  # [{category_code, weight_kg, co2e_avoided_kg}]

class UserRegistration(BaseModel):
    name: str
    phone: str
    email: str
    role: str

class UserLogin(BaseModel):
    phone: str

class TokenIssuanceResult(BaseModel):
    token_id: str
    work_order_id: str
    collector_id: str
    category_code: str
    tokens_issued: float
    inr_value: float
    collector_inr: float
    platform_inr: float
    municipal_inr: float
    formula_breakdown: dict
    token_mode: str
    blockchain_tx_hash: Optional[str]
    timestamp: str


# ─── Token formula ─────────────────────────────────────────────────────────

def compute_tokens(
    category_code: str,
    weight_kg: float,
    co2e_avoided_kg: float,
    supervisor_verified: bool,
    verification_score: float = 1.0,
) -> dict:
    """
    MIBA Token Formula v1.0

    tokens = base_rate
           × weight_kg
           × severity_multiplier      (1.0 – 2.35 based on env impact)
           × verification_multiplier  (1.0 if supervisor verified, 0.5 if not)
           × verification_score       (0.0–1.0 from Supervisor consensus)
           × carbon_bonus_multiplier  (1.0 + co2e_factor, capped at 1.5)
    """
    base_rate = BASE_TOKEN_RATES.get(category_code, 0.4)
    severity = SEVERITY_SCORES.get(category_code, 5)

    # Severity multiplier: 1.0 (severity=1) to 2.35 (severity=10)
    severity_multiplier = 1.0 + (severity - 1) * 0.15

    # Verification multiplier
    verification_multiplier = 1.0 if supervisor_verified else 0.5

    # Carbon bonus: scales with CO2e avoided, capped at 50% bonus
    co2e_bonus = min(co2e_avoided_kg * 0.02, 0.5)
    carbon_bonus_multiplier = 1.0 + co2e_bonus

    tokens = (
        base_rate
        * weight_kg
        * severity_multiplier
        * verification_multiplier
        * verification_score
        * carbon_bonus_multiplier
    )

    inr_rate = TOKEN_INR_RATES.get(category_code, 1.5)
    inr_value = tokens * inr_rate

    return {
        "tokens": round(tokens, 4),
        "inr_value": round(inr_value, 2),
        "formula_breakdown": {
            "base_rate": base_rate,
            "weight_kg": weight_kg,
            "severity_multiplier": round(severity_multiplier, 3),
            "verification_multiplier": verification_multiplier,
            "verification_score": verification_score,
            "carbon_bonus_multiplier": round(carbon_bonus_multiplier, 3),
            "inr_per_token": inr_rate,
        },
    }


# ─── Blockchain stub (Phase 1: off-chain; Phase 2: Polygon) ───────────────

async def issue_on_chain(category_code: str, tokens: float, collector_id: str) -> Optional[str]:
    """Phase 2: call Polygon ERC-1155 contract. Phase 1: returns None (off-chain)."""
    if TOKEN_MODE != "POLYGON":
        return None
    # TODO Phase 2: integrate web3.py + ERC-1155 contract
    # contract.functions.mint(collector_address, category_id, token_amount, b"").transact()
    return f"0x{'0' * 64}"  # placeholder tx hash


# ─── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    count = await db.db["ledger"].count_documents({})
    return {"status": "ok", "service": "token-engine", "mode": TOKEN_MODE, "ledger_size": count}

@app.get("/stats")
async def stats():
    """Public platform stats for the Home page."""
    user_count = await db.db["users"].count_documents({})
    report_count = await db.db["workorders"].count_documents({})
    return {"user_count": user_count, "report_count": report_count}

@app.post("/auth/register")
async def register(user: UserRegistration):
    """Register a new user (Citizen or Collector)."""
    logger.info("[token-engine] POST /auth/register — phone=%s role=%s", user.phone, user.role)
    existing = await db.db["users"].find_one({"phone": user.phone})
    if existing:
        logger.warning("[token-engine] Register FAILED — phone=%s already exists", user.phone)
        raise HTTPException(400, "User with this phone number already exists")
    
    user_dict = user.dict()
    user_dict["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.db["users"].insert_one(user_dict)
    logger.info("[token-engine] Register SUCCESS — phone=%s name=%s", user.phone, user.name)
    
    if "_id" in user_dict:
        del user_dict["_id"]
    return user_dict

@app.post("/auth/login")
async def login(req: UserLogin):
    """Simple phone-based login for Phase 1."""
    logger.info("[token-engine] POST /auth/login — phone=%s", req.phone)
    user = await db.db["users"].find_one({"phone": req.phone})
    if not user:
        logger.warning("[token-engine] Login FAILED — phone=%s not found", req.phone)
        raise HTTPException(404, "User not found. Please register.")
    logger.info("[token-engine] Login SUCCESS — phone=%s role=%s", req.phone, user.get('role'))
    
    if "_id" in user:
        del user["_id"]
    return user



@app.post("/tokens/issue", response_model=TokenIssuanceResult)
async def issue_tokens(req: TokenIssuanceRequest):
    """Issue MIBA tokens for a verified waste pickup."""
    result = compute_tokens(
        req.category_code, req.weight_kg, req.co2e_avoided_kg,
        req.supervisor_verified, req.verification_score,
    )

    tx_hash = await issue_on_chain(req.category_code, result["tokens"], req.collector_id)
    token_id = str(uuid.uuid4())

    record = {
        "token_id": token_id,
        "work_order_id": req.work_order_id,
        "collector_id": req.collector_id,
        "category_code": req.category_code,
        "tokens_issued": result["tokens"],
        "inr_value": result["inr_value"],
        "collector_inr": round(result["inr_value"] * SPLIT_COLLECTOR, 2),
        "platform_inr": round(result["inr_value"] * SPLIT_PLATFORM, 2),
        "municipal_inr": round(result["inr_value"] * SPLIT_MUNICIPAL, 2),
        "formula_breakdown": result["formula_breakdown"],
        "token_mode": TOKEN_MODE,
        "blockchain_tx_hash": tx_hash,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await db.db["ledger"].insert_one(record)


    logger.info("Tokens issued: %s tokens for WO %s collector %s",
                result["tokens"], req.work_order_id, req.collector_id)

    return TokenIssuanceResult(**record)


@app.post("/tokens/issue/bulk")
async def issue_bulk(req: BulkTokenRequest):
    """Issue tokens for all waste items in a completed work order."""
    issued = []
    total_tokens = 0.0
    total_inr = 0.0

    for item in req.waste_items:
        result = compute_tokens(
            item["category_code"], item["weight_kg"],
            item.get("co2e_avoided_kg", 0.0),
            req.verified, req.verification_score,
        )
        tx_hash = await issue_on_chain(item["category_code"], result["tokens"], req.collector_id)
        token_id = str(uuid.uuid4())
        record = {
            "token_id": token_id,
            "category_code": item["category_code"],
            "weight_kg": item["weight_kg"],
            "tokens_issued": result["tokens"],
            "inr_value": result["inr_value"],
            "blockchain_tx_hash": tx_hash,
        }
        await db.db["ledger"].insert_one({**record, "work_order_id": req.work_order_id, "collector_id": req.collector_id,
                        "token_mode": TOKEN_MODE, "timestamp": datetime.now(timezone.utc).isoformat(),
                        "formula_breakdown": result["formula_breakdown"]})

        issued.append(record)
        total_tokens += result["tokens"]
        total_inr += result["inr_value"]

    return {
        "work_order_id": req.work_order_id,
        "collector_id": req.collector_id,
        "items": issued,
        "totals": {
            "total_tokens": round(total_tokens, 4),
            "total_inr": round(total_inr, 2),
            "collector_payout_inr": round(total_inr * SPLIT_COLLECTOR, 2),
            "platform_fee_inr": round(total_inr * SPLIT_PLATFORM, 2),
            "municipal_share_inr": round(total_inr * SPLIT_MUNICIPAL, 2),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/tokens/ledger/{collector_id}")
async def get_ledger(collector_id: str, limit: int = 50):
    cursor = db.db["ledger"].find({"collector_id": collector_id}).sort("timestamp", -1).limit(limit)
    records = await cursor.to_list(length=limit)
    
    total = sum(r["tokens_issued"] for r in records)
    total_inr = sum(r["inr_value"] for r in records)
    
    for r in records:
        if "_id" in r:
            del r["_id"]
            
    return {
        "collector_id": collector_id,
        "total_tokens": round(total, 4),
        "total_inr_earned": round(total_inr, 2),
        "collector_payout_inr": round(total_inr * SPLIT_COLLECTOR, 2),
        "records": records,
    }



@app.get("/tokens/rates")
def get_rates():
    return {
        "token_inr_rates": TOKEN_INR_RATES,
        "base_token_rates": BASE_TOKEN_RATES,
        "revenue_split": {
            "collector": SPLIT_COLLECTOR,
            "platform": SPLIT_PLATFORM,
            "municipal": SPLIT_MUNICIPAL,
        },
        "token_mode": TOKEN_MODE,
    }
