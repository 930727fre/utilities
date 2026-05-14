import { useState, useEffect, useCallback } from 'react';
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import {
  Paper, Title, Text, Button, Group, Stack,
  Progress, ActionIcon, ThemeIcon, SimpleGrid, Box, Transition
} from '@mantine/core';
import PageShell from '../components/PageShell';
import { IconCheck, IconLamp, IconCopy } from '@tabler/icons-react';
import { useNavigate } from 'react-router-dom';
import { notifications } from '@mantine/notifications';
import { api } from '../api';
import type { Card } from '../types';

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace';

const RATINGS = [
  { label: 'Again', value: 1 },
  { label: 'Hard',  value: 2 },
  { label: 'Good',  value: 3 },
  { label: 'Easy',  value: 4 },
] as const;

export default function ReviewPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const reviewCard = useMutation({
    mutationFn: ({ id, rating }: { id: string; rating: number }) => api.reviewCard(id, rating),
    retry: 3,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['stats'] });
      queryClient.invalidateQueries({ queryKey: ['queue'] });
    },
    onError: () => {
      notifications.show({ title: 'Sync failed', message: 'Rating could not be saved. The card will reappear next session.' });
    },
  });

  const [reviewQueue, setReviewQueue] = useState<Card[]>([]);
  const [isFlipped, setIsFlipped] = useState(false);
  const [copied, setCopied] = useState(false);

  const { data: queueData, isLoading } = useQuery({
    queryKey: ['queue'],
    queryFn: api.getQueue,
    staleTime: 0,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });

  // Sync from backend: append any cards not already in the local queue.
  // Rated cards are removed locally and rescheduled by the backend; they
  // reappear here once their due time passes (e.g. "Again" → +1m relearning).
  useEffect(() => {
    if (!queueData) return;
    setReviewQueue(prev => {
      const prevIds = new Set(prev.map(c => c.id));
      const newCards = queueData.cards.filter(c => !prevIds.has(c.id));
      return newCards.length === 0 ? prev : [...prev, ...newCards];
    });
  }, [queueData]);

  const currentCard = reviewQueue[0];

  const handleRate = useCallback((rating: number) => {
    if (!currentCard) return;
    reviewCard.mutate({ id: currentCard.id, rating });
    setReviewQueue(prev => prev.slice(1));
    setIsFlipped(false);
  }, [currentCard, reviewCard]);

  useEffect(() => {
    if (!currentCard) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.code === 'Space' || e.code === 'Enter') {
        e.preventDefault();
        setIsFlipped(true);
      } else if (isFlipped) {
        const idx = parseInt(e.key) - 1;
        if (idx >= 0 && idx < RATINGS.length) handleRate(RATINGS[idx].value);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isFlipped, currentCard, handleRate]);

  if (isLoading) {
    return (
      <PageShell scroll="centered">
        <Stack align="center" gap="xs">
          <Progress value={100} w={200} size="sm" radius="xl" animated color="gray" />
          <Text c="#aeaeb2" fw={600} size="xs" style={{ letterSpacing: 1.5, fontFamily: MONO }}>LOADING SESSION...</Text>
        </Stack>
      </PageShell>
    );
  }

  if (!isLoading && reviewQueue.length === 0) {
    return (
      <PageShell scroll="centered" size="xs">
        <Paper radius={20} p={40} withBorder
          style={{ background: '#2c2c2e', borderColor: '#3a3a3c', textAlign: 'center' }}>
          <Stack align="center" gap="xl">
            <ThemeIcon size={80} radius="xl" variant="filled" style={{ backgroundColor: '#3a3a3c', color: '#e8e3d9' }}>
              <IconCheck size={40} />
            </ThemeIcon>
            <Box>
              <Title order={2} c="#e8e3d9">Session Complete</Title>
              <Text c="#aeaeb2" mt="sm">All cards have been reviewed.</Text>
            </Box>
            <Button
              size="lg"
              radius="md"
              fullWidth
              onClick={() => navigate('/')}
              style={{ background: '#c79968', color: '#1c1c1e', border: 'none', fontWeight: 600 }}
            >
              Back to Dashboard
            </Button>
          </Stack>
        </Paper>
      </PageShell>
    );
  }

  return (
    <PageShell scroll="locked">
      <Stack gap="lg" style={{ flex: 1, minHeight: 0 }}>
        <Text
          c="#e8e3d9"
          style={{ cursor: 'pointer', fontFamily: MONO, fontSize: 26, lineHeight: 1, alignSelf: 'flex-start' }}
          onClick={() => navigate('/')}
          title="Back"
          aria-label="Back"
          role="button"
        >
          ←
        </Text>

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
        <Transition mounted={!!currentCard} transition="slide-up" duration={400}>
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
                background: '#2c2c2e',
                border: '1px solid #3a3a3c',
                boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
                overflowY: 'auto',
                position: 'relative',
              }}
            >
              <Stack align="center" gap={0} p={{ base: 'lg', sm: 40 }} style={{ flex: 1, justifyContent: 'center' }}>
                <Title order={1} ta="center" style={{ fontSize: 'clamp(2rem, 8vw, 3.5rem)', color: '#e8e3d9', letterSpacing: '-0.5px', lineHeight: 1.2 }}>
                  {currentCard?.word}
                </Title>

                {currentCard?.sentence && (
                  <Text c="#aeaeb2" ta="center" size="lg" mt="xl" fs="italic" style={{ maxWidth: '90%' }}>
                    "{currentCard.sentence}"
                  </Text>
                )}

                {isFlipped && (
                  <Box mt={40} pt={30} style={{ borderTop: '1px solid #3a3a3c', width: '100%' }}>
                    <Group gap="xs" justify="center" mb="xs" opacity={0.5}>
                      <IconLamp size={14} />
                      <Text size="xs" fw={800} tt="uppercase" style={{ fontFamily: MONO }}>Definition / Note</Text>
                    </Group>
                    <Text ta="center" size="xl" fw={600} c="#e8e3d9" style={{ wordBreak: 'break-word' }}>
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
                        style={{ color: '#aeaeb2', border: '1px solid #3a3a3c' }}
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
              onClick={() => handleRate(value)}
              tabIndex={isFlipped ? 0 : -1}
              styles={{
                root: {
                  backgroundColor: '#2c2c2e',
                  border: '1px solid #3a3a3c',
                  height: 70,
                },
                inner: { flexDirection: 'column', gap: 2 },
              }}
            >
              <Text fw={800} size="sm" c="#e8e3d9">{label}</Text>
              <Text size="xs" c="#aeaeb2" fw={500} style={{ fontFamily: MONO }}>[{i + 1}]</Text>
            </Button>
          ))}
        </SimpleGrid>
        </Box>
      </Stack>
    </PageShell>
  );
}
