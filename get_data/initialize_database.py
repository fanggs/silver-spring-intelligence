"""Increment 1 entry point: create the empty, readable CivicLens database."""
from get_data.database import initialize_database

if __name__ == "__main__":
    initialize_database()
    print("Created data/silverspring.db")
