from redash.models import db, QueryResultStore

if __name__ == '__main__':
    with db.database.transaction():
        if not QueryResultStore.table_exists():
            QueryResultStore.create_table()

    db.close_db(None)
