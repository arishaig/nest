-- Empty every table the app created on its schema-creation start, seed rows
-- included. Replaces the Servarr wiki's per-app DELETE lists, which target
-- older majors: a seeded table left out would make pgloader reject our rows
-- on PK collision while the seed row survived.
\set ON_ERROR_STOP on
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', r.tablename);
  END LOOP;
END $$;
