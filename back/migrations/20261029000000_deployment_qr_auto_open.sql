ALTER TABLE tenant ADD COLUMN IF NOT EXISTS qr_auto_open boolean NOT NULL DEFAULT false;
