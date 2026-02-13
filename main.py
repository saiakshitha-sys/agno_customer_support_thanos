from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Any
import os
import uvicorn
from agents import get_support_team, knowledge, agent_os_db, run_session_evals

app = FastAPI(title="Agno AgentOS - Thanos CS")

# --- Validation Logic (Phase 2A) ---

def validate_user_context(payload: dict):
    # Map UserRole to permission flags (matching n8n logic)
    ROLE_MAP = {
        "PILOT": {"perm": "1", "superperm": 0, "allperm": 0},
        "CUSTOMER_SUPPORT": {"perm": "2", "superperm": 0, "allperm": 0},
        "TECHNICIAN": {"perm": "3", "superperm": 0, "allperm": 0},
        "LOG_ANALYSIS_ENGINEER": {"perm": "4", "superperm": 0, "allperm": 0},
        "CUSTOMER_ADMIN": {"perm": "0", "superperm": "1", "allperm": 0},
        "SENIOR_CS": {"perm": "0", "superperm": "2", "allperm": 0},
        "ADMIN": {"perm": "0", "superperm": "0", "allperm": 1},
        "USER": {"perm": "2", "superperm": 0, "allperm": 0} # Fallback
    }
    
    # Normalize role to uppercase
    role = str(payload.get("userRole", "USER")).upper()
    perms = ROLE_MAP.get(role, ROLE_MAP["USER"])
    
    return {
        "message": payload.get("message"),
        "conversationId": payload.get("conversationId"),
        "sessionId": payload.get("sessionId"),
        "accessToken": payload.get("accessToken"),
        "userName": payload.get("userName", "Unknown"),
        "userEmail": payload.get("userEmail", "Not provided"),
        "userId": payload.get("userId", "unknown"),
        "tenantId": payload.get("tenantId", "Thanos"),
        "userRole": role,
        "customToken": payload.get("customToken", None),
        **perms
    }

# --- API Endpoints ---

class ChatPayload(BaseModel):
    message: str
    conversationId: Optional[str] = None
    sessionId: Optional[str] = None
    userRole: Optional[str] = "USER"
    userName: Optional[str] = "Unknown"
    userEmail: Optional[str] = "Not provided"
    userId: Optional[str] = "unknown"
    tenantId: Optional[str] = "Thanos"
    accessToken: Optional[str] = None
    customToken: Optional[str] = None

@app.post("/chat")
async def handle_chat(payload: ChatPayload):
    try:
        # 0. Manual Auth Check (Because token is in payload, not header)
        if not payload.accessToken:
             # Allow requests without token if strictly local/testing, but for prod enforce it:
             # For now, log warning if missing, or raise error if you want strict security.
             # raise HTTPException(status_code=401, detail="Missing accessToken")
             print("[WARN] No accessToken provided in payload")
        # else:
        #      # UNDO: Do not validate accessToken manually here, as it might be in a different format (e.g. HS256)
        #      # than what verify_token_manual (RS256) expects. We rely on customToken for secure validation.
        #      verify_token_manual(payload.accessToken)

        # 1. Validate & Map Context
        context = validate_user_context(payload.dict())
        
        # 2. Get Agent Team
        team = get_support_team(context)
        
        # 3. Resolve Session ID
        # Priority: conversationId > sessionId. user request: "take the session id from the user and not anything created"
        session_id = context.get("conversationId") or context.get("sessionId")
        if not session_id:
             raise HTTPException(status_code=400, detail="conversationId or sessionId is required")

        # 4. Load Long-Term History from public.cs_messages
        from agents import load_history_from_db
        db_history = load_history_from_db(session_id)
        
        # 5. Run Agent
        team.last_user_msg = context["message"] # For sync tool
        
        # Prepare Metadata for AgentOS Visibility
        run_metadata = {
            "tenantId": context.get("tenantId"),
            "userId": context.get("userId"),
            "userRole": context.get("userRole"),
            "userName": context.get("userName"),
            "userEmail": context.get("userEmail"),
            "source": "external_frontend"
        }
        
        response = team.run(
            context["message"],
            session_id=session_id,
            user_id=context.get("userId") or context.get("userEmail"),
            metadata=run_metadata,
            additional_input=db_history # Inject history from public.cs_messages
        )
        
        # 5. Get Memories
        memories = []
        try:
            if team.memory_manager:
                # Retrieve memories for the user
                user_id_lookup = context.get("userId") or context.get("userEmail")
                raw_memories = team.memory_manager.get_user_memories(user_id=user_id_lookup)
                
                for m in raw_memories:
                    # Check for to_dict (Pydantic or custom)
                    if hasattr(m, "to_dict"):
                        memories.append(m.to_dict())
                    elif hasattr(m, "dict"):
                        memories.append(m.dict())
                    elif hasattr(m, "model_dump"):
                        memories.append(m.model_dump())
                    elif isinstance(m, dict):
                        memories.append(m)
                    elif hasattr(m, "memory"): # Fallback to just the memory text
                        memories.append({"memory": m.memory, "topics": getattr(m, "topics", [])})
                    else:
                        memories.append({"memory": str(m)})
        except Exception as e:
            print(f"Memory retrieval failed: {e}")

        # 6. Format response
        output_text = response.content
        # Removed TotalToken display logic
        # if "TotalToken:" not in output_text and response.metrics:
        #     output_text += f"\nTotalToken: {response.metrics.total_tokens}"
        
        return {
            "output": output_text,
            "sessionId": session_id,
            "conversationId": context.get("conversationId"),
            "memory": memories,
            "eval": None # Explicitly None as per-message evals are disabled
        }
        
    except HTTPException as e:
        raise e
    except Exception as e:
        # Print stack trace to logs for debugging
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

class EvalPayload(BaseModel):
    sessionId: str
    userId: Optional[str] = None

@app.post("/session/eval")
async def handle_session_eval(payload: EvalPayload):
    try:
        result = run_session_evals(payload.sessionId, payload.userId)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "agno-agent-thanos"}

from agno.os import AgentOS
from agno.os.config import (
    AgentOSConfig,
    EvalsConfig,
    MemoryConfig,
    SessionConfig,
    DatabaseConfig,
    AuthorizationConfig,
)
from agno.os.middleware import JWTMiddleware
import jwt

# --- Security Configuration ---
# Extracted from jwks-2a929edd-80d1-4ef5-8870-071576a0a3ad-1770095379255.json
JWT_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAlyzDeV2OfRox1+zBg58B
zcf/IilEzDE9tZXAq7p0Na2T9QjJap9bSjvruCNBAUQ9u/xpT/HC/ndiuJI/lH/p
ATKkejSwylL4+Cd7CeYhTfpiE05CoTvqNRQ45S6GTp0LBah6QoD6ENVe+NAvXOlE
MqZr/xwVHwo3Ttd3s/dq1OtAitJtugQ891Ig29SRB22wjBRoZiS0b2fOa3tnGeBu
50RVOBqOLJvFfl2+1YvWKrzzid+ouapcQUzwU67r7a3/OU3HXfY6mOPYxNXdUh2M
8ZGTl+263+uTxb6hhj1XSlfgqSWBGmI1B+xgZ/a6HKKs3zRad9BoUz6F86CZa784
bQIDAQAB
-----END PUBLIC KEY-----"""

def verify_token_manual(token: str):
    """Manually verify JWT token using the public key."""
    try:
        # Verify signature and expiration
        # Audience check is disabled by default here unless we know the audience
        jwt.decode(token, JWT_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False})
    except jwt.ExpiredSignatureError:
        print("[AUTH ERROR] Token has expired")
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError as e:
        print(f"[AUTH ERROR] Invalid token: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    except Exception as e:
        print(f"[AUTH ERROR] Unexpected error: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

# --- AgentOS Control Plane Integration ---
# Create a default agent for the Control Plane (Admin Context)
playground_context = {
    "userName": "AgentOS Admin",
    "userEmail": "admin@agno.com",
    "perm": "0",
    "superperm": "0",
    "allperm": "1",
    "userRole": "ADMIN",
    "conversationId": "agentos-session",
    "tenantId": "Thanos"
}
playground_agent = get_support_team(playground_context)

# Initialize AgentOS with the existing FastAPI app and custom UI configuration
agent_os = AgentOS(
    agents=[playground_agent],
    db=agent_os_db,     # Unified Production DB (ai schema / agno_* tables)
    knowledge=[knowledge],
    base_app=app,
    tracing=True,      # Enable Tracking
    telemetry=True,    # Enable Metrics
    authorization=False, # Disable internal RBAC to use custom middleware
    config=AgentOSConfig(
        evals=EvalsConfig(
            display_name="Thanos Agent Evals",
            dbs=[DatabaseConfig(db_id=agent_os_db.id)],
        ),
        memory=MemoryConfig(
            display_name="Thanos User Memories",
            dbs=[DatabaseConfig(db_id=agent_os_db.id)],
        ),
        session=SessionConfig(
            display_name="Thanos Chat Sessions",
            dbs=[DatabaseConfig(db_id=agent_os_db.id)],
        )
    )
)

# Get the final application with all AgentOS routes
app = agent_os.get_app()

# Add JWT Middleware manually to allow excluding /chat
app.add_middleware(
    JWTMiddleware,
    verification_keys=[JWT_PUBLIC_KEY],
    algorithm="RS256",
    authorization=True,
    excluded_route_paths=["/chat", "/health", "/docs", "/openapi.json"]
)

if __name__ == "__main__":
    # Ensure it binds to localhost for AgentOS security
    uvicorn.run(app, host="127.0.0.1", port=8000)
