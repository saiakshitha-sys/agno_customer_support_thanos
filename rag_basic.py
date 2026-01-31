import os
from typing import Optional
from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.google import Gemini
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.pgvector import PgVector, SearchType
from agno.knowledge.embedder.google import GeminiEmbedder

# Load environment variables
load_dotenv()

# Configuration
DB_URL = os.getenv("DATABASE_URL")
if DB_URL:
    DB_URL = DB_URL.strip().strip("'").strip('"')
else:
    # Use the static URL from create_table_public.py if not in .env
    DB_URL = "postgresql+psycopg://postgres:julley%40qwe@40.192.63.10:5432/postgres"

TABLE_NAME = "cs_agno_vectordb1"

# 1. Setup Vector DB & Knowledge Base
vector_db = PgVector(
    table_name=TABLE_NAME,
    schema="ai",
    db_url=DB_URL,
    search_type=SearchType.hybrid,
    embedder=GeminiEmbedder(id="text-embedding-004", dimensions=768)
)

knowledge = Knowledge(
    vector_db=vector_db
)

# 2. Load Grounding Prompt from prompt.md
def load_instructions(user_context: dict) -> list:
    try:
        with open("prompt.md", "r") as f:
            content = f.read()
        
        # Replace placeholders with context values
        # The prompt uses {{$json.userName}} style placeholders
        instructions = content.replace("{{$json.userName}}", user_context.get("userName", "User"))
        instructions = instructions.replace("{{$json.userEmail}}", user_context.get("userEmail", "Unknown"))
        instructions = instructions.replace("{{$json.perm}}", str(user_context.get("perm", 0)))
        instructions = instructions.replace("{{$json.allperm}}", str(user_context.get("allperm", 0)))
        instructions = instructions.replace("{{$json.superperm}}", str(user_context.get("superperm", 0)))
        
        return [instructions]
    except Exception as e:
        print(f"Error loading prompt.md: {e}")
        return ["You are a helpful customer support agent."]

def get_rag_agent(user_context: dict):
    """
    Initializes a single RAG agent grounded by retrieval and the prompt.md instructions.
    """
    instructions = load_instructions(user_context)
    
    agent = Agent(
        name="Julley Support (Basic RAG)",
        model=Gemini(id="gemini-2.5-flash"),
        knowledge=knowledge,
        search_knowledge=True,
        instructions=instructions,
        add_history_to_context=True,
        num_history_runs=5,
        # Performance/Response settings
        markdown=True
    )
    return agent

if __name__ == "__main__":
    # Mock user context for demonstration
    test_context = {
        "userName": "Thanos Pilot",
        "userEmail": "pilot@thanos.ai",
        "perm": 1,
        "allperm": 0,
        "superperm": 0
    }
    
    print("🚀 Initializing Julley Support RAG Agent...")
    print(f"📡 Connecting to Database table: {TABLE_NAME}")
    
    agent = get_rag_agent(test_context)
    
    print("\n--- RAG Agent Ready ---")
    print(f"Context: {test_context['userName']} (Perm: {test_context['perm']})")
    print("Type 'exit' to quit.\n")
    
    while True:
        try:
            user_input = input("User: ")
            if user_input.lower() in ["exit", "quit"]:
                break
            
            # Print response with direct token usage reporting if possible
            # Note: Agno's print_response doesn't show custom info easily during streaming
            # We will use run() to get back context if needed, but for prompt compliance
            # we rely on the instructions to the agent itself.
            
            response = agent.run(user_input)
            
            # Output the response content
            print(f"\nAgent: {response.content}")
            
            # Compliance with prompt rule: "output, totalToken usage count"
            # Agno response object usually contains metrics
            if hasattr(response, "metrics") and response.metrics:
                tokens = response.metrics.get("token_usage", {}).get("total_tokens", "N/A")
                print(f"\nTotalToken: {tokens}")
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"\nAn error occurred: {e}")