"""Ask the analytics DB a natural-language question.

Usage:
    python run_query.py "which clients increased fixed-income activity?"
    python run_query.py            # interactive mode
"""
import sys

from fo.ai.agent import ask
from fo.config import get_settings
from fo.db.database import connect


def main() -> None:
    conn = connect(get_settings().db_path)

    if len(sys.argv) > 1:
        print(ask(conn, " ".join(sys.argv[1:])))
        return

    print("Ask a question (empty line to quit):")
    while True:
        try:
            q = input("> ").strip()
        except EOFError:
            break
        if not q:
            break
        print(ask(conn, q))


if __name__ == "__main__":
    main()