from pathlib import Path


def validate_config(path: Path | None = None) -> dict:
    """Validate all config files. Return list of errors/warnings."""
    from .loader import get_config_dir
    config_dir = path or get_config_dir()
    errors = []
    tokens_path = config_dir / "config" / "tokens.yaml"
    if tokens_path.exists():
        import jsonschema

        # Ensure TOKENS_SCHEMA is properly defined in tokens_schema.py
        from .tokens_schema import TOKENS_SCHEMA
        try:
            with open(tokens_path) as f:
                data = yaml.safe_load(f) or {{}}
            jsonschema.validate(instance=data, schema=TOKENS_SCHEMA)
        except jsonschema.ValidationError as e:
            errors.append({"file": str(tokens_path), "error": str(e)})
        except FileNotFoundError:
            # This case should ideally not happen if tokens_path.exists() is true,
            # but included for robustness.
            errors.append({"file": str(tokens_path), "error": "File not found during validation."})
        except yaml.YAMLError as e:
            errors.append({"file": str(tokens_path), "error": f"Invalid YAML format: {e}"})

    # Add other config file validation here if needed

    return {"valid": len(errors) == 0, "errors": errors}
