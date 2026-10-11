-- Empty every table the app created on its schema-creation start, seed rows
-- included. Replaces the Servarr wiki's per-app DELETE lists, which target
-- older majors: a seeded table left out would make pgloader reject our rows
-- on PK collision while the seed row survived.
-- Tables named in the pgm.keep_tables setting (comma-separated, set by
-- migrate-local.sh) are app-owned and left untouched, e.g. Seerr's
-- TypeORM `migrations`.
\set ON_ERROR_STOP on
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
             AND tablename <> ALL (string_to_array(
                   coalesce(current_setting('pgm.keep_tables', true), ''), ',')) LOOP
    EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', r.tablename);
  END LOOP;
END $$;
