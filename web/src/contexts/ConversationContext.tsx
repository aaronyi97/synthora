import { createContext, useContext } from 'react';
import type { useConversations } from '@/hooks/useConversations';

type ConversationContextType = ReturnType<typeof useConversations>;

export const ConversationContext = createContext<ConversationContextType | null>(null);

export function useConversationContext(): ConversationContextType {
  const ctx = useContext(ConversationContext);
  if (!ctx) throw new Error('useConversationContext must be used within ConversationProvider');
  return ctx;
}
