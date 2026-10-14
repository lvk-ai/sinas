import { useQuery } from '@tanstack/react-query';
import { apiClient } from './api';

// The effective ratecard of the last 30 days, shared by Admin › Usage and the
// LLM Providers page. Admins only (sinas.usage.read:all): for anyone else it
// is a quiet 403 and `data` stays undefined, so what uses it hides itself.
export const RATECARD_QUERY_KEY = ['usage-ratecard'];

export function useRatecard() {
  return useQuery({
    queryKey: RATECARD_QUERY_KEY,
    queryFn: () => apiClient.getRatecard({ days: 30 }),
    retry: false,
  });
}

// A per-token price as USD per million tokens
export function perMillion(value: number | undefined): string {
  if (value == null) return '—';
  return `$${Number((value * 1_000_000).toPrecision(4))}`;
}
