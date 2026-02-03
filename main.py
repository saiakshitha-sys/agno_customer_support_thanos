from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
import os
import uvicorn
from agents import get_support_team, knowledge, agent_os_db

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

@app.post("/chat")
async def handle_chat(payload: ChatPayload):
    try:
        # 1. Validate & Map Context
        context = validate_user_context(payload.dict())
        
        # 2. Get Agent Team
        team = get_support_team(context)
        
        # 3. Run Agent
        # Priority: conversationId > sessionId. user request: "take the session id from the user and not anything created"
        session_id = context.get("conversationId") or context.get("sessionId")
        if not session_id:
             raise HTTPException(status_code=400, detail="conversationId or sessionId is required")
             
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
            metadata=run_metadata
        )
        
        # 4. Sync turn to backend
        try:
            from agents import sync_turn_to_backend
            sync_turn_to_backend(team, response)
        except Exception as e:
            print(f"Post-run sync failed: {e}")

        # 5. Format response
        output_text = response.content
        if "TotalToken:" not in output_text and response.metrics:
            output_text += f"\nTotalToken: {response.metrics.total_tokens}"
        
        return {
            "output": output_text,
            "sessionId": session_id,
            "conversationId": context.get("conversationId")
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "agno-agent-thanos"}

from agno.os import AgentOS

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

# Initialize AgentOS with the existing FastAPI app
agent_os = AgentOS(
    agents=[playground_agent],
    db=agent_os_db,     # Unified Production DB (ai schema / agno_* tables)
    knowledge=[knowledge],
    base_app=app,
    tracing=True,      # Enable Tracking
    telemetry=True,    # Enable Metrics
)

# Get the final application with all AgentOS routes
app = agent_os.get_app()

if __name__ == "__main__":
    # Ensure it binds to localhost for AgentOS security
    uvicorn.run(app, host="127.0.0.1", port=8000)
