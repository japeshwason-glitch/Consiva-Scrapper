# Lead gen pipeline

Run order: stages/01 through stages/07, each one a cron job reading rows at
one status and advancing them to the next. See db.py for the shared
read/advance/fail helpers every stage script uses.
