import { AlertTriangle, RefreshCw } from 'lucide-react';
import { errorMessage } from './hooks';

/** A read that failed (not a 403): say so, rather than showing an empty state. */
export function LoadError({ what, error, onRetry, retrying }: {
  what: string;
  error: unknown;
  onRetry: () => void;
  retrying?: boolean;
}) {
  return (
    <div className="text-center py-12 card">
      <AlertTriangle className="w-12 h-12 text-red-400 mx-auto mb-3" />
      <h3 className="text-lg font-medium text-gray-100 mb-1">Couldn't load {what}</h3>
      <p className="text-sm text-red-400 mb-4 whitespace-pre-wrap">{errorMessage(error, 'The request failed')}</p>
      <button onClick={onRetry} disabled={retrying} className="btn btn-secondary inline-flex items-center">
        <RefreshCw className={`w-4 h-4 mr-2 ${retrying ? 'animate-spin' : ''}`} />
        Try again
      </button>
    </div>
  );
}
