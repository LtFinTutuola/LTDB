"""
scripts/manage_users.py
-----------------------
Emergency command line tool for direct user administration and disaster recovery.
Operates directly on the SQLite database using SQLAlchemy sessions.

Usage examples:
  python scripts/manage_users.py list
  python scripts/manage_users.py reset-password --username admin --password NuovaPassword123
  python scripts/manage_users.py create-user --username mario --password Segreta123 --role base
  python scripts/manage_users.py revoke-all
"""
import sys
import argparse
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.database import SessionLocal
from src.core.security import hash_password
from src.models.users import User, UserRole


def list_users():
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.username.asc()).all()
        print(f"\n{'ID':<38} {'USERNAME':<20} {'ROLE':<10} {'ACTIVE':<8} {'TOKEN_VER':<10}")
        print("-" * 90)
        for u in users:
            print(f"{u.id:<38} {u.username:<20} {u.role:<10} {str(u.is_active):<8} {u.token_version:<10}")
        print(f"\nTotale: {len(users)} utenti registrati.\n")
    finally:
        db.close()


def reset_password(username: str, password: str):
    db = SessionLocal()
    try:
        clean_user = username.strip().lower()
        user = db.query(User).filter(User.username == clean_user).first()
        if not user:
            print(f"ERRORE: Utente '{clean_user}' non trovato.")
            sys.exit(1)

        user.hashed_password = hash_password(password)
        user.token_version += 1
        db.commit()
        print(f"SUCCESSO: Password reimpostata per '{clean_user}' e sessioni invalidate (token_version={user.token_version}).")
    finally:
        db.close()


def create_user(username: str, password: str, role: str):
    db = SessionLocal()
    try:
        clean_user = username.strip().lower()
        existing = db.query(User).filter(User.username == clean_user).first()
        if existing:
            print(f"ERRORE: Utente '{clean_user}' esiste già.")
            sys.exit(1)

        new_user = User(
            username=clean_user,
            hashed_password=hash_password(password),
            role=role,
            is_active=True,
            token_version=1,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        print(f"SUCCESSO: Utente '{clean_user}' creato con ruolo '{role}'.")
    finally:
        db.close()


def revoke_all():
    db = SessionLocal()
    try:
        db.query(User).update({User.token_version: User.token_version + 1}, synchronize_session=False)
        db.commit()
        print("SUCCESSO: Token version incrementata per tutti gli utenti. Tutte le sessioni sono state revocate.")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="LTDB User Management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    subparsers.add_parser("list", help="Elenca tutti gli utenti registrati")

    # reset-password
    rp = subparsers.add_parser("reset-password", help="Reimposta la password di un utente")
    rp.add_argument("--username", required=True, help="Nome utente")
    rp.add_argument("--password", required=True, help="Nuova password")

    # create-user
    cu = subparsers.add_parser("create-user", help="Crea un nuovo utente")
    cu.add_argument("--username", required=True, help="Nome utente")
    cu.add_argument("--password", required=True, help="Password iniziale")
    cu.add_argument("--role", choices=["admin", "base"], default="base", help="Ruolo utente (default: base)")

    # revoke-all
    subparsers.add_parser("revoke-all", help="Revoca immediatamente tutte le sessioni attive")

    args = parser.parse_args()

    if args.command == "list":
        list_users()
    elif args.command == "reset-password":
        reset_password(args.username, args.password)
    elif args.command == "create-user":
        create_user(args.username, args.password, args.role)
    elif args.command == "revoke-all":
        revoke_all()


if __name__ == "__main__":
    main()
