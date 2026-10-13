import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../../lib/api';
import type { PackageRepository } from '../../types';

/** The Sinas team's repository, as POST /package-repositories/official adds it. */
export const OFFICIAL_REPOSITORY = {
  name: 'sinas-official',
  url: 'https://github.com/sinas-platform/packages',
};

const normalizeUrl = (url: string) => url.trim().replace(/\/+$/, '').toLowerCase();

/** Already added? The backend treats the name or the URL as a match. */
export function hasOfficialRepository(repositories: PackageRepository[] | undefined): boolean {
  return !!repositories?.some(
    (r) => r.name === OFFICIAL_REPOSITORY.name || normalizeUrl(r.url) === normalizeUrl(OFFICIAL_REPOSITORY.url),
  );
}

/** A failed request's message: the API's detail, with a 422's validation
 * list read as its messages rather than shown as JSON. */
export function errorMessage(error: unknown, fallback: string): string {
  const detail = (error as any)?.response?.data?.detail;
  if (Array.isArray(detail)) {
    return detail.map((d: any) => (typeof d?.msg === 'string' ? d.msg : JSON.stringify(d))).join('\n');
  }
  return (error as any)?.message || fallback;
}

export const isForbidden =(error: unknown): boolean => (error as any)?.response?.status === 403;

/** Repositories are admin-only; for anyone else the query ends in a 403,
 * which the page explains in place (no retry, no toast). */
export function usePackageRepositories() {
  return useQuery({
    queryKey: ['package-repositories'],
    queryFn: () => apiClient.listPackageRepositories(),
    retry: (failureCount, error) => !isForbidden(error) && failureCount < 1,
  });
}

/** Everything a repository change can move: the repositories themselves
 * (status, counts), the catalog, and installed packages' update badges. */
export function useInvalidatePackageData() {
  const queryClient = useQueryClient();
  return () => {
    queryClient.invalidateQueries({ queryKey: ['package-repositories'] });
    queryClient.invalidateQueries({ queryKey: ['package-catalog'] });
    queryClient.invalidateQueries({ queryKey: ['packages'] });
  };
}

export function useAddOfficialRepository() {
  const invalidate = useInvalidatePackageData();
  return useMutation({
    mutationFn: () => apiClient.addOfficialPackageRepository(),
    onSettled: invalidate,
  });
}

/** "5m ago", "3h ago", "2d ago" */
export function timeAgo(iso: string): string {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
