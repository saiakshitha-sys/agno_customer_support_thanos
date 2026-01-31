import os
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.google import Gemini
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.pgvector import PgVector, SearchType
from agno.knowledge.embedder.google import GeminiEmbedder
from agno.db.postgres import PostgresDb
from agno.tools import tool
import requests

load_dotenv()

# Configuration
DB_URL = os.getenv("DATABASE_URL")
if DB_URL:
    DB_URL = DB_URL.strip().strip("'").strip('"')

TABLE_NAME = "cs_agno_vectordb1"
SESSION_TABLE = "cs_agno_longterm_memory"

# --- 1. RAG CONNECTION & ROBUST FILTERING ---
embedder = GeminiEmbedder(id="text-embedding-004", dimensions=768)
vector_db = PgVector(
    table_name=TABLE_NAME,
    schema="ai",
    db_url=DB_URL,
    search_type=SearchType.vector,
    embedder=embedder
)

# Shared Session DB
session_db = PostgresDb(db_url=DB_URL, session_table=SESSION_TABLE, db_schema="ai")

def get_robust_filter(agent: Agent):
    """Priority: allperm=1 > superperm > perm."""
    perm = str(getattr(agent, "perm", "0"))
    superperm = str(getattr(agent, "superperm", "0"))
    allperm = str(getattr(agent, "allperm", "0"))
    
    if allperm == "1":
        return {"allperm": 1}
    elif superperm != "0":
        return {"superperm": superperm}
    elif perm != "0":
        return {"perm": perm}
    return {}

# --- 2. BACKEND ACTION TOOLS (ALIGNED WITH N8N SCHEMA) ---

@tool
def create_support_ticket(agent: Agent, title: str, main_issue: str, summary: str) -> str:
    """
    Creates a formal support ticket in the Thanos Staging Backend.
    Call this ONLY when the user confirms 'ticket details confirmed'.
    """
    url = "https://stagebackend.julleyonline.co.in/api/v1/customer-support/n8n/ticket"
    
    # Extract History for Backend
    history = []
    try:
        history_msgs = agent.get_chat_history()
        history = [{"role": getattr(m, "role", "unknown"), "content": getattr(m, "content", "")} for m in history_msgs]
    except Exception as e:
        print(f"[TOOL ERROR] History retrieval failed: {e}")
        history = []

    payload = {
        "conversationId": agent.session_id,
        "userId": getattr(agent, "userId", "unknown"),
        "tenantId": getattr(agent, "tenantId", "Thanos"),
        "sessionId": agent.session_id,
        "title": title,
        "description": main_issue,
        "summary": summary,
        "userName": getattr(agent, "userName", "Unknown"),
        "userEmail": getattr(agent, "userEmail", "Not provided"),
        "conversationHistory": history,
        "timestamp": datetime.now().isoformat(),
        "metadata": {
            "source": "agno-agent",
            "version": "1.0",
            "ticketCreated": True,
            "role": getattr(agent, "userRole", "USER")
        }
    }
    
    token = getattr(agent, "accessToken", "")
    n8n_key = os.environ.get("N8N_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-API-Key": n8n_key
    }
    
    try:
        print(f"\n[BACKEND] Hitting staging ticket API for: {agent.session_id}")
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"[BACKEND] Response Code: {response.status_code}")
        
        if response.status_code in [200, 201]:
            print("[BACKEND] Ticket Created Successfully")
            return "SUCCESS: Ticket details confirmed. Our team will follow up shortly."
        
        print(f"[BACKEND] Error Details: {response.text}")
        return f"FAILURE: Backend returned {response.status_code}. Details: {response.text}"
    except Exception as e:
        print(f"[BACKEND] Exception: {str(e)}")
        return f"FAILURE: API Connection Error: {str(e)}"

@tool
def save_conversation_summary(agent: Agent, summary: str, topic: str, main_issue: str) -> str:
    """
    Saves the final conversation summary to the backend.
    IMPORTANT: The agent MUST generate the 'summary', 'topic', and 'main_issue' itself by analyzing the chat history.
    DO NOT ask the user for these details. Identify the start and end of the conversation to provide a duration-aware summary.
    """
    url = "https://stagebackend.julleyonline.co.in/api/v1/customer-support/n8n/conversation"
    
    payload = {
        "conversationId": agent.session_id,
        "userId": getattr(agent, "userId", "unknown"),
        "tenantId": getattr(agent, "tenantId", "Thanos"),
        "sessionId": agent.session_id,
        "topic": topic,
        "description": main_issue,
        "summary": summary,
        "userName": getattr(agent, "userName", "Unknown"),
        "userEmail": getattr(agent, "userEmail", "Not provided"),
        "messageCount": len(agent.get_chat_history()) if hasattr(agent, "get_chat_history") else 0,
        "closedAt": datetime.now().isoformat(),
        "lastMessageAt": datetime.now().isoformat()
    }
    
    token = getattr(agent, "accessToken", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-API-Key": os.environ.get("N8N_API_KEY", "")
    }
    
    try:
        print(f"\n[BACKEND] Saving summary to: {url}")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[BACKEND] Response Code: {response.status_code}")
        
        status = "Synced." if response.status_code in [200, 201] else f"Error {response.status_code}"
        if response.status_code not in [200, 201]:
            print(f"[BACKEND] Error Details: {response.text}")
            
        return f"CONVERSATION SUMMARY:\n{summary}\n\n[Status: {status}]"
    except Exception as e:
        print(f"[BACKEND] Summary Exception: {str(e)}")
        return f"CONVERSATION SUMMARY:\n{summary}\n\n[Status: Sync Error {str(e)}]"

@tool
def search_documentation(agent: Agent, query: str) -> str:
    """Search knowledge base based on user permissions."""
    meta_filter = get_robust_filter(agent)
    print(f"\n[RAG] Searching: '{query}' | Filter: {meta_filter}")
    results = vector_db.search(query=query, limit=5, filters=meta_filter)
    if not results:
        return "No specific documentation found for your request at your permission level."
    return "\n\n".join([r.content for r in results])

# --- 3. AGENT FACTORY ---

def get_support_team(user_context: dict):
    with open("prompt.md", "r") as f:
        prompt_content = f.read()
    
    # Dynamic String Injection
    instructions = prompt_content.replace("{{$json.userName}}", user_context.get("userName", "User"))
    instructions = instructions.replace("{{$json.userEmail}}", user_context.get("userEmail", "Not provided"))
    instructions = instructions.replace("{{$json.perm}}", str(user_context.get("perm", "0")))
    instructions = instructions.replace("{{$json.allperm}}", str(user_context.get("allperm", "0")))
    instructions = instructions.replace("{{$json.superperm}}", str(user_context.get("superperm", "0")))

    support_agent = Agent(
        name="Julley Support",
        model=Gemini(id="gemini-2.5-flash"),
        db=session_db,
        instructions=[
            "--- AGENT CORE BEHAVIOR ---",
            "1. You are a STATEFUL agent. Refer to Chat History for context.",
            "2. If the user query is technical, SEARCH DOCUMENTS immediately.",
            "3. TICKET LOGIC: When details are confirmed (Step 4), YOU MUST CALL 'create_support_ticket' immediately.",
            "4. NEVER say 'ticket details confirmed' without calling the tool first.",
            "5. COMPLETION LOGIC: When the user says thank you or the issue is resolved, CALL 'save_conversation_summary' immediately without asking the user for any details. Generate the summary, topic, and main issue yourself from history.",
            "6. NEVER ask the user: 'What was the topic?' or 'Can you provide a summary?'. You are responsible for this.",
            "7. TotalToken: Include this at the end of every response.",
            instructions,
        ],
        tools=[search_documentation, create_support_ticket, save_conversation_summary],
        add_history_to_context=True,
        num_history_runs=10
    )
    
    for key, value in user_context.items():
        setattr(support_agent, key, value)
    
    # User Request: "take the session id from the user" and "relate to the window"
    # We explicitly set the session_id to the user's provided ID.
    # The 'num_history_runs=10' above ensures we load the last 10 interactions (Window) for this specific Session ID.
    support_agent.session_id = user_context.get("conversationId") or user_context.get("sessionId")
    return support_agent

def sync_turn_to_backend(agent: Any, response: Any):
    url = "https://stagebackend.julleyonline.co.in/api/v1/customer-support/n8n/messages"
    user_msg = getattr(agent, "last_user_msg", "...")
    payload = {
        "conversationId": agent.session_id,
        "userId": getattr(agent, "userId", "unknown"),
        "tenantId": getattr(agent, "tenantId", "Thanos"),
        "sessionId": agent.session_id,
        "userName": getattr(agent, "userName", "Unknown"),
        "userEmail": getattr(agent, "userEmail", "Not provided"),
        "timestamp": datetime.now().isoformat(),
        "messages": [
            {"senderType": "user", "content": user_msg},
            {"senderType": "assistant", "content": response.content}
        ]
    }
    headers = {
        "Authorization": f"Bearer {getattr(agent, 'accessToken', '')}",
        "X-API-Key": os.environ.get("N8N_API_KEY", "")
    }
    try:
        requests.post(url, json=payload, headers=headers, timeout=5)
    except Exception as e:
        print(f"[SYNC] Error syncing messages: {e}")
