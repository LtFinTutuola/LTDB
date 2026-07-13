import yaml
import json
import os
from src.graph import build_graph

def main():
    # Load config
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        print("config.yaml not found!")
        return

    # Load api config
    try:
        with open("api.yaml", "r") as f:
            api_config = yaml.safe_load(f)
    except FileNotFoundError:
        api_config = {"api_key": "", "endpoint_url": ""}

    # Initialize graph
    app = build_graph()
    
    # Initial state
    file_path = config.get("input_file_path", "")
    if file_path and not os.path.isabs(file_path):
        file_path = os.path.join(os.getcwd(), file_path)

    initial_state = {
        "api_key": api_config.get("api_key", ""),
        "endpoint_url": api_config.get("endpoint_url", ""),
        "file_path": file_path,
        "file_format": config.get("input_file_format", "PDF"),
        "document_content": None,
        "classification": "",
        "extracted_data": None,
        "schema_valid": False,
        "logic_valid": False,
        "validation_errors": [],
        "final_output": None
    }
    
    print("Starting LangGraph Execution...")
    
    # Run the graph
    for output in app.stream(initial_state):
        for node_name, state in output.items():
            print(f"Completed node: {node_name}")
            
    # Print final output
    final_state = output.get(list(output.keys())[0])
    print("\n--- FINAL OUTPUT PAYLOAD ---")
    print(json.dumps(final_state.get("final_output"), indent=2))

if __name__ == "__main__":
    main()
