DEFAULT_TOKENS = {
    "ast_grep": {"max_input_tokens": 4096, "max_output_tokens": 16384},
    "ast_edit": {"max_input_tokens": 8192, "max_output_tokens": 4096},
    "ast_read": {"max_input_tokens": 2048, "max_output_tokens": 32768},
    "semantic_search": {"max_input_tokens": 512, "max_output_tokens": 8192},
}

# Placeholder for the actual TOKENS_SCHEMA
# This will be defined based on the full schema requirements.
TOKENS_SCHEMA = {
    "type": "object",
    "properties": {
        "ast_grep": {
            "type": "object",
            "properties": {
                "max_input_tokens": {"type": "integer", "minimum": 1},
                "max_output_tokens": {"type": "integer", "minimum": 1}
            },
            "required": ["max_input_tokens", "max_output_tokens"]
        },
        "ast_edit": {
            "type": "object",
            "properties": {
                "max_input_tokens": {"type": "integer", "minimum": 1},
                "max_output_tokens": {"type": "integer", "minimum": 1}
            },
            "required": ["max_input_tokens", "max_output_tokens"]
        },
        "ast_read": {
            "type": "object",
            "properties": {
                "max_input_tokens": {"type": "integer", "minimum": 1},
                "max_output_tokens": {"type": "integer", "minimum": 1}
            },
            "required": ["max_input_tokens", "max_output_tokens"]
        },
        "semantic_search": {
            "type": "object",
            "properties": {
                "max_input_tokens": {"type": "integer", "minimum": 1},
                "max_output_tokens": {"type": "integer", "minimum": 1}
            },
            "required": ["max_input_tokens", "max_output_tokens"]
        }
    },
    "additionalProperties": False
}

DEFAULT_TOKENS_PATH = "src/ast_tools/config/tokens.yaml" # relative
