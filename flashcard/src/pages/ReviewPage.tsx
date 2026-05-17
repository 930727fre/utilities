import { useState, useEffect, useCallback, useRef } from 'react';
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import {
  Paper, Title, Text, Button, Group, Stack,
  ActionIcon, SimpleGrid, Box, Transition
} from '@mantine/core';
import PageShell from '../components/PageShell';
import { IconCheck, IconLamp, IconCopy } from '@tabler/icons-react';
import { useNavigate } from 'react-router-dom';
import { notifications } from '@mantine/notifications';
import { api } from '../api';
import type { Card } from '../types';


const RATINGS = [
  { label: 'Again', value: 1 },
  { label: 'Hard',  value: 2 },
  { label: 'Good',  value: 3 },
  { label: 'Easy',  value: 4 },
] as const;

type Phase = 'initializing' | 'due' | 'transition' | 'new' | 'done';

export default function ReviewPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [pendingCount, setPendingCount] = useState(0);
  const [isConnected, setIsConnected] = useState(true);
  const canNavigate = pendingCount === 0;
  const canRate = isConnected && canNavigate;

  useEffect(() => {
    const es = new EventSource('/api/heartbeat');
    es.onopen    = () => setIsConnected(true);
    es.onmessage = () => setIsConnected(true);
    es.onerror   = () => setIsConnected(false);
    return () => es.close();
  }, []);

  const [phase, setPhase] = useState<Phase>('initializing');
  const [dueQueue, setDueQueue] = useState<Card[]>([]);
  const [newQueue, setNewQueue] = useState<Card[]>([]);
  const [isFlipped, setIsFlipped] = useState(false);
  const [copied, setCopied] = useState(false);

  // Always holds a fresh closure over current phase/queue state.
  // Called from onSuccess so the queue advances only after server confirms.
  const advanceQueue = useRef<() => void>(() => {});
  useEffect(() => {
    advanceQueue.current = () => {
      setIsFlipped(false);
      if (phase === 'due') {
        const next = dueQueue.slice(1);
        setDueQueue(next);
        if (next.length === 0) setPhase(newQueue.length > 0 ? 'transition' : 'done');
      } else {
        const next = newQueue.slice(1);
        setNewQueue(next);
        if (next.length === 0) setPhase('done');
      }
    };
  }, [phase, dueQueue, newQueue]);

  const reviewCard = useMutation({
    mutationFn: ({ id, rating }: { id: string; rating: number }) => api.reviewCard(id, rating),
    retry: 1,
    onMutate:  () => setPendingCount(c => c + 1),
    onSettled: () => setPendingCount(c => c - 1),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['stats'] });
      advanceQueue.current();
    },
    onError: () => {
      notifications.show({ title: 'Sync failed', message: 'Rating could not be saved. The card will reappear next session.' });
    },
  });

  const { data: queueData } = useQuery({
    queryKey: ['queue'],
    queryFn: api.getQueue,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });

  useEffect(() => {
    if (!queueData) return;
    setDueQueue(queueData.due);
    setNewQueue(queueData.new);
    setPhase(queueData.due.length > 0 ? 'due' : queueData.new.length > 0 ? 'new' : 'done');
  }, [queueData]);

  const currentCard = phase === 'due' ? dueQueue[0] : phase === 'new' ? newQueue[0] : undefined;

  const handleRate = useCallback((rating: number) => {
    if (!currentCard) return;
    reviewCard.mutate({ id: currentCard.id, rating });
  }, [currentCard, reviewCard]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (phase === 'transition') {
        setPhase(newQueue.length > 0 ? 'new' : 'done');
        return;
      }
      if (!currentCard) return;
      if (e.code === 'Space' || e.code === 'Enter') {
        e.preventDefault();
        setIsFlipped(true);
      } else if (isFlipped && canRate) {
        const idx = parseInt(e.key) - 1;
        if (idx >= 0 && idx < RATINGS.length) handleRate(RATINGS[idx].value);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [phase, isFlipped, currentCard, handleRate, newQueue, canRate]);

  if (phase === 'transition') {
    return (
      <PageShell scroll="locked">
        <Stack gap="lg" style={{ flex: 1, minHeight: 0 }}>
          <Paper
            p={0}
            radius={24}
            onClick={() => setPhase(newQueue.length > 0 ? 'new' : 'done')}
            style={{
              flex: 1,
              minHeight: 0,
              display: 'flex',
              flexDirection: 'column',
              background: 'var(--card)',
              border: '1px solid var(--border)',
              boxShadow: 'var(--shadow)',
              cursor: 'pointer',
              userSelect: 'none',
            }}
          >
            <Stack align="center" gap={0} p={{ base: 'lg', sm: 40 }} style={{ flex: 1, justifyContent: 'center' }}>
              <Text c="var(--text-dim)" style={{ fontFamily: 'var(--mono)', fontSize: 11, letterSpacing: 2, textTransform: 'uppercase' }}>
                due · done
              </Text>
              <Title order={1} ta="center" mt="xl" style={{ fontSize: 'clamp(2rem, 8vw, 3.5rem)', color: 'var(--text-h)', letterSpacing: '-0.5px', lineHeight: 1.2 }}>
                new words
              </Title>
              <Text c="var(--text-dim)" size="xs" mt={48} style={{ fontFamily: 'var(--mono)' }}>
                tap · [space]
              </Text>
            </Stack>
          </Paper>
        </Stack>
      </PageShell>
    );
  }

  if (phase === 'done') {
    return (
      <PageShell scroll="locked">
        <Stack gap="lg" style={{ flex: 1, minHeight: 0 }}>
          <Paper
            p={0}
            radius={24}
            style={{
              flex: 1,
              minHeight: 0,
              display: 'flex',
              flexDirection: 'column',
              background: 'var(--card)',
              border: '1px solid var(--border)',
              boxShadow: 'var(--shadow)',
            }}
          >
            <Stack align="center" gap={0} p={{ base: 'lg', sm: 40 }} style={{ flex: 1, justifyContent: 'center' }}>
              <Text c="var(--text-dim)" style={{ fontFamily: 'var(--mono)', fontSize: 11, letterSpacing: 2, textTransform: 'uppercase' }}>
                session complete
              </Text>
              <Title order={1} ta="center" mt="xl" style={{ fontSize: 'clamp(2rem, 8vw, 3.5rem)', color: 'var(--text-h)', letterSpacing: '-0.5px', lineHeight: 1.2 }}>
                done
              </Title>
            </Stack>
            <Box p={{ base: 'lg', sm: 40 }} pt={0}>
              <Button
                size="lg"
                radius="md"
                fullWidth
                disabled={!canNavigate}
                onClick={() => navigate('/')}
                style={{ background: canNavigate ? 'var(--accent)' : 'var(--raised)', color: canNavigate ? 'var(--bg)' : 'var(--text-dim)', border: 'none', fontWeight: 600, transition: 'background 0.2s, color 0.2s' }}
              >
                {canNavigate ? 'Back to Dashboard' : <span className="glyph-pulse">○</span>}
              </Button>
            </Box>
          </Paper>
        </Stack>
      </PageShell>
    );
  }

  return (
    <PageShell scroll="locked">
      <Stack gap="lg" style={{ flex: 1, minHeight: 0 }}>
        <Box
          onClick={!isFlipped ? () => setIsFlipped(true) : undefined}
          style={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: 'var(--mantine-spacing-lg)',
            cursor: !isFlipped ? 'pointer' : 'default',
          }}
        >
          <Transition mounted={!!currentCard} transition="slide-up" duration={600}>
            {(styles) => (
              <Paper
                p={0}
                radius={24}
                style={{
                  ...styles,
                  flex: 1,
                  minHeight: 0,
                  display: 'flex',
                  flexDirection: 'column',
                  background: 'var(--card)',
                  border: '1px solid var(--border)',
                  boxShadow: 'var(--shadow)',
                  overflowY: 'auto',
                  position: 'relative',
                }}
              >
                <Stack align="center" gap={0} p={{ base: 'lg', sm: 40 }} style={{ flex: 1, justifyContent: 'center' }}>
                  <Title order={1} ta="center" style={{ fontSize: 'clamp(2rem, 8vw, 3.5rem)', color: 'var(--text-h)', letterSpacing: '-0.5px', lineHeight: 1.2 }}>
                    {currentCard?.word}
                  </Title>

                  {currentCard?.sentence && (
                    <Text c="var(--text)" ta="center" size="lg" mt="xl" fs="italic" style={{ maxWidth: '90%' }}>
                      "{currentCard.sentence}"
                    </Text>
                  )}

                  {isFlipped && (
                    <Box mt={40} pt={30} style={{ borderTop: '1px solid var(--border)', width: '100%' }}>
                      <Group gap="xs" justify="center" mb="xs" opacity={0.5}>
                        <IconLamp size={14} />
                        <Text size="xs" fw={800} tt="uppercase" style={{ fontFamily: 'var(--mono)' }}>Definition / Note</Text>
                      </Group>
                      <Text ta="center" size="xl" fw={600} c="var(--text-h)" style={{ wordBreak: 'break-word' }}>
                        {currentCard?.note || "No notes provided."}
                      </Text>
                      <Group justify="center" mt="lg">
                        <ActionIcon
                          variant="subtle"
                          size="lg"
                          radius="xl"
                          title={copied ? 'Prompt copied' : 'Copy prompt'}
                          onClick={(e) => {
                            e.stopPropagation();
                            navigator.clipboard.writeText(`幫我造 ${currentCard?.word} 的例句`);
                            setCopied(true);
                            setTimeout(() => setCopied(false), 2000);
                          }}
                          style={{ color: 'var(--text)', border: '1px solid var(--border)' }}
                        >
                          {copied ? <IconCheck size={16} /> : <IconCopy size={16} />}
                        </ActionIcon>
                      </Group>
                    </Box>
                  )}
                </Stack>
              </Paper>
            )}
          </Transition>

          <SimpleGrid
            cols={{ base: 2, sm: 4 }}
            spacing="sm"
            style={{
              visibility: isFlipped ? 'visible' : 'hidden',
              pointerEvents: isFlipped ? 'auto' : 'none',
            }}
            aria-hidden={!isFlipped}
          >
            {RATINGS.map(({ label, value }, i) => (
              <Button
                key={label}
                variant="filled"
                size="xl"
                radius="md"
                disabled={!canRate}
                onClick={() => handleRate(value)}
                tabIndex={isFlipped && canRate ? 0 : -1}
                styles={{
                  root: {
                    backgroundColor: 'var(--card)',
                    border: '1px solid var(--border)',
                    height: 70,
                    opacity: canRate ? 1 : 0.3,
                    transition: 'opacity 0.2s',
                  },
                  inner: { flexDirection: 'column', gap: 2 },
                }}
              >
                <Text fw={800} size="sm" c="var(--text-h)">{label}</Text>
                <Text size="xs" c="var(--text)" fw={500} style={{ fontFamily: 'var(--mono)' }}>[{i + 1}]</Text>
              </Button>
            ))}
          </SimpleGrid>
        </Box>
      </Stack>
    </PageShell>
  );
}
