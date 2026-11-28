-- Aantal pagina's per boek (aangevuld vanuit de Bol-catalogus door bol_paginas_aanvullen.py; voor de feed naar Google).
ALTER TABLE books ADD COLUMN IF NOT EXISTS pages INTEGER;
