from redash.models import db, HistoricalQueryResult

if __name__ == '__main__':
    with db.database.transaction():
        if not HistoricalQueryResult.table_exists():
            HistoricalQueryResult.create_table()

    db.close_db(None)
