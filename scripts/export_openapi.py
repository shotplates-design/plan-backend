"""Write the live OpenAPI schema to openapi.json (for the frontend agent)."""

from pathlib import Path

from app.main import create_app


def main() -> None:
    app = create_app()
    path = Path(__file__).resolve().parents[1] / "openapi.json"
    path.write_text(app.openapi_json() if hasattr(app, "openapi_json") else "")
    # FastAPI: serialize via jsonable encoder
    import json

    path.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print(f"Wrote {path} ({path.stat().st_size} bytes, {len(app.openapi()['paths'])} paths)")


if __name__ == "__main__":
    main()
