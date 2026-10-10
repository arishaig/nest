-- Move every owned sequence past max(column). Same block as the 2026-09-30
-- Jellyfin repair (docs/jellyfin-upgrades.md); data-only loads leave
-- sequences where TRUNCATE ... RESTART IDENTITY put them.
\set ON_ERROR_STOP on
DO $$ DECLARE r record; mx bigint; lv bigint; BEGIN
  FOR r IN SELECT s.relname seq, t.relname tbl, a.attname col
           FROM pg_class s JOIN pg_depend d ON d.objid = s.oid
           JOIN pg_class t ON t.oid = d.refobjid
           JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
           JOIN pg_namespace n ON n.oid = s.relnamespace
           WHERE s.relkind = 'S' AND n.nspname = 'public' LOOP
    EXECUTE format('SELECT max(%I) FROM public.%I', r.col, r.tbl) INTO mx;
    EXECUTE format('SELECT last_value FROM public.%I', r.seq) INTO lv;
    IF mx IS NOT NULL AND mx >= lv THEN
      PERFORM setval(format('public.%I', r.seq), mx);
    END IF;
  END LOOP;
END $$;
