-- Mith Hera Call Assistant server — Supabase tables.
-- Run this once in the Supabase SQL editor (project already connected
-- for Jarvis). Recording files go to the `call-recordings` bucket.

create table if not exists call_records (
  id uuid primary key default gen_random_uuid(),
  call_uuid text unique not null,
  from_number text,
  to_number text,
  started_at timestamptz,
  ended_at timestamptz,
  duration_seconds int default 0,
  transcript jsonb default '[]'::jsonb,
  summary text,
  recording_url text,
  recording_inbound_url text,
  recording_outbound_url text,
  transfer_requested boolean default false,
  transferred boolean default false,
  created_at timestamptz default now()
);

create table if not exists call_notifications (
  id text primary key,
  kind text,
  title text,
  body text,
  call_id text,
  created_at timestamptz,
  read boolean default false
);

-- Storage bucket for call recordings (both legs, never deleted).
insert into storage.buckets (id, name, public)
values ('call-recordings', 'call-recordings', true)
on conflict (id) do nothing;
