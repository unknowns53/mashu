-- Store retirement conflicts on nominations for human review (5.1, 5.3).
ALTER TABLE nomination ADD COLUMN conflicts UUID[];
