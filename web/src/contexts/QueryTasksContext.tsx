import { createContext, useContext } from 'react';
import type { useQueryTasks } from '@/hooks/useQueryTasks';

type QueryTasksContextType = ReturnType<typeof useQueryTasks>;

export const QueryTasksContext = createContext<QueryTasksContextType | null>(null);

export function useQueryTasksContext(): QueryTasksContextType {
  const ctx = useContext(QueryTasksContext);
  if (!ctx) throw new Error('useQueryTasksContext must be used within QueryTasksProvider');
  return ctx;
}
