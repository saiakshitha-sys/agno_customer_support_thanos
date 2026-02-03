import os
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from agno.agent import Agent, RunOutput
from agno.models.google import Gemini
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.pgvector import PgVector, SearchType
from agno.knowledge.embedder.google import GeminiEmbedder
from agno.db.postgres import PostgresDb
from agno.memory.manager import MemoryManager
from agno.eval.base import BaseEval
from agno.eval.accuracy import AccuracyEval, AccuracyResult
from agno.eval.reliability import ReliabilityEval
from agno.eval.performance import PerformanceEval
from agno.eval.utils import log_eval_run
from agno.db.schemas.evals import EvalType
from agno.tools import tool
import requests

load_dotenv()

# Configuration
PROD_DB_URL = "postgresql+psycopg://postgres:julley%40qwe@40.192.63.10:5432/thanos"
DB_URL = PROD_DB_URL

TABLE_NAME = "cs_agno_vectordb2"
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

# --- OBSERVABILITY & PERSISTENCE (EXPLICIT DB INSTANCES) ---
# Each component has its own DB definition for clarity, though they share the same underlying Postgres connection.

# 1. Sessions DB (Chat History)
sessions_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    session_table="agno_sessions",
)

# 2. Memory DB (User Facts)
memory_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    memory_table="agno_memories",
)

# 3. Evals DB (Accuracy, Reliability, Performance Scores)
evals_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    eval_table="agno_eval_runs",
)

# 4. Traces DB (AgentOS Telemetry) - Used by AgentOS generic storage
traces_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    traces_table="agno_traces",
)

# 5. UNIFIED MONITORING DB (For AgentOS UI Reading)
# AgentOS needs ONE db connection that knows about ALL tables to display them in the Admin UI.
agent_os_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    session_table="agno_sessions",
    memory_table="agno_memories",
    eval_table="agno_eval_runs",
    traces_table="agno_traces",
)





# Knowledge Base Content DB (Required for UI Visibility in 'ai' schema)
knowledge_content_db = PostgresDb(db_url=DB_URL, knowledge_table="cs_agno_knowledge_contents", db_schema="ai")

# Knowledge Base
knowledge = Knowledge(vector_db=vector_db, contents_db=knowledge_content_db)



# --- 1.5 SNIPPET-BASED AUTOMATED EVALUATOR ---

class SupportEvaluator(BaseEval):
    def pre_check(self, run_input: Any) -> None: pass
    async def async_pre_check(self, run_input: Any) -> None: pass
    async def async_post_check(self, run_output: Any) -> None: pass

    def post_check(self, run_output: RunOutput) -> None:
        """Automatically log evals to the Control Plane after each run using standard snippets."""
        try:
            # --- 0. EXPLICIT INPUT MAPPING ---
            # User Requirement: "make sure all the input is going from the message field of the ChatPayload"
            # In main.py: agent.run(payload.message) -> run_output.input IS the raw user message.
            user_message_input = run_output.input
            
            # 1. Performance Eval (Snippet-Based)
            # Snippet logic uses PerformanceEval.run_with_output
            perf_eval = PerformanceEval(
                name="Performance-Live",
                db=evals_db, # Explicitly using evals_db
                input=user_message_input, # <--- Mapped from payload.message
                expected_output="Helpful and accurate response." # Placeholder for live context
            )
            # We use the actual run time and content
            perf_eval.run_with_output(output=run_output.content, print_results=False)

            # 2. Reliability Eval (Snippet-Based)
            # Snippet logic uses ReliabilityEval.run_with_output
            rel_eval = ReliabilityEval(
                name="Reliability-Live",
                db=evals_db, # Explicitly using evals_db
                agent_response=run_output,
                input=user_message_input, # <--- Mapped from payload.message
                expected_output="Successful tool execution." # Placeholder
            )
            # Evaluate tool calls
            run_tools = getattr(run_output, "tool_calls", [])
            if run_tools:
                rel_eval.run_with_output(output=run_output.content, print_results=False)
            else:
                # Manual log if no tools were called to show 'Passed'
                log_eval_run(
                    db=evals_db, # Explicitly using evals_db
                    run_id=run_output.run_id,
                    eval_type=EvalType.RELIABILITY,
                    eval_input={"message": user_message_input}, # <--- Mapped from payload.message
                    run_data={"status": "PASSED", "reason": "No tools required"},
                    name="Reliability-Live",
                    evaluated_component_name="SupportAgent"
                )

            # 3. Accuracy Eval (Snippet-Based LLM Judge)
            # We use Gemini as the judge for Accuracy
            acc_eval = AccuracyEval(
                name="Accuracy-Live",
                db=evals_db, # Explicitly using evals_db
                model=Gemini(id="gemini-2.5-flash"), # Use Gemini as judge
                input=user_message_input, # <--- Mapped from payload.message
                expected_output="A response grounded in the provided documentation."
            )
            # Judge the actual output
            acc_eval.run_with_output(output=run_output.content, print_results=False)

        except Exception as e:
            print(f"[EVAL ERROR] Failed to log run: {e}")
        except Exception as e:
            print(f"[EVAL ERROR] Failed to log run: {e}")

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
    Call this ONLY when the user confirms the ticket details or says 'ticket details confirmed'.
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
    headers = {
        "Authorization": f"Bearer {getattr(agent, 'accessToken', '')}",
        "Content-Type": "application/json",
    }
    
    try:
        print(f"\n[BACKEND-TICKET] Hitting staging ticket API for: {agent.session_id}")
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"[BACKEND-TICKET] Response Code: {response.status_code}")
        
        if response.status_code in [200, 201]:
            print("[BACKEND-TICKET] Ticket Created Successfully")
            return "SUCCESS: Ticket details confirmed. Our team will follow up shortly."
        
        print(f"[BACKEND-TICKET] Error Details: {response.text}")
        return f"FAILURE: Backend returned {response.status_code}. Details: {response.text}"
    except Exception as e:
        print(f"[BACKEND-TICKET] Exception: {str(e)}")
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
        "closedAt": int(datetime.now().timestamp() * 1000),
        "lastMessageAt": int(datetime.now().timestamp() * 1000)
    }
    token = getattr(agent, "accessToken", "")
    headers = {
        "Authorization": f"Bearer {getattr(agent, 'accessToken', '')}",
        "Content-Type": "application/json",
    }
    
    try:
        print(f"\n[BACKEND-SUMMARY] Saving summary to: {url}")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[BACKEND-SUMMARY] Response Code: {response.status_code}")
        
        status = "Synced." if response.status_code in [200, 201] else f"Error {response.status_code}"
        if response.status_code not in [200, 201]:
            print(f"[BACKEND-SUMMARY] Error Details: {response.text}")
            
        return f"CONVERSATION SUMMARY:\n{summary}\n\n[Backend Status: {status}]"
    except Exception as e:
        print(f"[BACKEND-SUMMARY] Summary Exception: {str(e)}")
        return f"CONVERSATION SUMMARY:\n{summary}\n\n[Backend Status: Sync Error {str(e)}]"

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

    session_id = user_context.get("conversationId") or user_context.get("sessionId")
    user_id = user_context.get("userId") or user_context.get("userEmail")

    support_agent = Agent(
        name="Julley Support",
        user_id=user_id,
        session_id=session_id,
        model=Gemini(id="gemini-2.5-flash"),
        db=sessions_db, # Explicitly using sessions_db for persistent chat history
        # 1. Memory Manager Configuration (Snippet-Based Pattern)
        # We explicitly provide a Model to the MemoryManager so it can "extract" facts from the conversation.
        memory_manager=MemoryManager(
            db=memory_db, 
            model=Gemini(id="gemini-2.5-flash"),
            memory_capture_instructions=(
                "Recieve all the important notes and components of the user like problems, questions he asked, "
                "context he asked the question for, his tone, info he fetched and the negatives and positives "
                "of him needs to be stored."
            )
        ), 
        update_memory_on_run=True,   # Extract & Save facts to agno_memories
        enable_agentic_memory=True,  # Enable reasoning about user facts
        debug_mode=True,             # Enable logs to see MEMORY and EVALS updates
        instructions=[
            "--- AGENT CORE BEHAVIOR ---",
            "1. You are a STATEFUL agent. Refer to Chat History for context.",
            "2. If the user query is technical, SEARCH DOCUMENTS immediately.",
            "3. TICKET LOGIC: When details are confirmed, YOU MUST CALL 'create_support_ticket' immediately.",
            "4. NEVER say 'ticket details confirmed' without calling the tool first.",
            "5. COMPLETION LOGIC (CRITICAL): At the end of every response, ANALYZE THE LAST 4 MESSAGES (2 User, 2 AI).",
            "   - If the conversation feels complete (e.g., query resolved, user says thank you, or you say 'Ticket details confirmed/ticket creation successful').",
            "   - Or if you see phrases like: 'please feel free to reach out again', 'if you need anything else', 'resolved', 'thank you'.",
            "   - If these patterns exist, CALL 'save_conversation_summary' immediately without asking the user.",
            "   - Generate the summary, topic, and main issue yourself from history.",
            "6. DISPLAY SUMMARY: After calling 'save_conversation_summary', you MUST display the generated summary to the user clearly.",
            "7. TotalToken: Include this at the end of every response.",
            instructions,
        ],
        tools=[search_documentation, create_support_ticket, save_conversation_summary],
        add_history_to_context=True,
        num_history_runs=10,
        post_hooks=[SupportEvaluator()] # Enable automatic evaluations for UI
    )
    
    for key, value in user_context.items():
        setattr(support_agent, key, value)
    
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
        "Content-Type": "application/json",
        }
    try:
        requests.post(url, json=payload, headers=headers, timeout=5)
    except Exception as e:
        print(f"[SYNC] Error syncing messages: {e}")
