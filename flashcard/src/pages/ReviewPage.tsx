import { useState, useEffect, useCallback, useRef } from 'react';
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import {
  Container, Paper, Title, Text, Button, Group, Stack,
  Progress, ActionIcon, ThemeIcon, SimpleGrid, Center, Box, Badge, Transition
} from '@mantine/core';
import { IconArrowLeft, IconCheck, IconLamp, IconBulb } from '@tabler/icons-react';
import { useNavigate } from 'react-router-dom';
import { notifications } from '@mantine/notifications';
import { api } from '../api';
import type { Card } from '../types';

const RATINGS = [
  { label: 'Again', value: 1, color: '#ff6b6b', bg: 'rgba(255,107,107,0.1)' },
  { label: 'Hard',  value: 2, color: '#ffd43b', bg: 'rgba(255,212,59,0.1)'  },
  { label: 'Good',  value: 3, color: '#51cf66', bg: 'rgba(81,207,102,0.1)'  },
  { label: 'Easy',  value: 4, color: '#339af0', bg: 'rgba(51,154,240,0.1)'  },
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
      notifications.show({ title: 'Sync failed', message: 'Rating could not be saved. The card will reappear next session.', color: 'red' });
    },
  });

  const [reviewQueue, setReviewQueue] = useState<Card[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [isFlipped, setIsFlipped] = useState(false);
  const initialized = useRef(false);

  const { data: queueData, isLoading } = useQuery({
    queryKey: ['queue'],
    queryFn: api.getQueue,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });

  useEffect(() => {
    if (queueData && !initialized.current) {
      initialized.current = true;
      setReviewQueue(queueData.cards);
      setTotalCount(queueData.cards.length);
    }
  }, [queueData]);

  const currentCard = reviewQueue[0];
  const reviewed = totalCount - reviewQueue.length;

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
      <Center h="100vh" bg="#0a0c14">
        <Stack align="center" gap="xs">
          <Progress value={100} w={200} size="sm" radius="xl" animated color="blue" />
          <Text c="dimmed" fw={600} size="xs" style={{ letterSpacing: 1.5 }}>LOADING SESSION...</Text>
        </Stack>
      </Center>
    );
  }

  if (!isLoading && reviewQueue.length === 0) {
    return (
      <Container size="xs" py={100}>
        <Center>
          <Paper radius={20} p={40} withBorder bg="#0e1f16" style={{ borderColor: '#1e5c36', textAlign: 'center' }}>
            <Stack align="center" gap="xl">
              <ThemeIcon size={80} radius="xl" color="green" variant="light">
                <IconCheck size={40} />
              </ThemeIcon>
              <Box>
                <Title order={2} c="#e8eaf0">Session Complete!</Title>
                <Text c="dimmed" mt="sm">Great job! All cards have been reviewed. 🔥</Text>
              </Box>
              <Button
                size="lg"
                radius="md"
                fullWidth
                onClick={() => navigate('/')}
                style={{ background: 'linear-gradient(135deg, #1a3d28, #2a5c3e)', border: 'none' }}
              >
                Back to Dashboard
              </Button>
            </Stack>
          </Paper>
        </Center>
      </Container>
    );
  }

  const themeColor = currentCard?.state === 0 ? '#4dbb7a' : '#4a8fff';
  const themeBg = currentCard?.state === 0 ? 'rgba(77, 187, 122, 0.05)' : 'rgba(74, 143, 255, 0.05)';

  return (
    <Container size="sm" py="xl" px="md" style={{ paddingBottom: 'max(24px, env(safe-area-inset-bottom))' }}>
      <Stack gap="lg">
        <Group justify="space-between">
          <ActionIcon variant="subtle" onClick={() => navigate('/')} size="xl" c="dimmed">
            <IconArrowLeft size={28} />
          </ActionIcon>
          <Box style={{ textAlign: 'right' }}>
            <Text fw={700} c="dimmed" style={{ fontFamily: 'JetBrains Mono', fontSize: 18 }}>
              <span style={{ color: themeColor }}>{reviewed + 1}</span> / {totalCount}
            </Text>
          </Box>
        </Group>

        <Progress
          value={(reviewed / totalCount) * 100}
          size="xs"
          radius="xl"
          color={currentCard?.state === 0 ? 'green' : 'blue'}
          styles={{ root: { backgroundColor: '#1a1d2e' } }}
        />

        <Transition mounted={!!currentCard} transition="slide-up" duration={400}>
          {(styles) => (
            <Paper
              p={0}
              radius={24}
              onClick={() => !isFlipped && setIsFlipped(true)}
              style={{
                ...styles,
                minHeight: 400,
                display: 'flex',
                flexDirection: 'column',
                cursor: isFlipped ? 'default' : 'pointer',
                background: `linear-gradient(145deg, #161b2c 0%, #0d111d 100%)`,
                border: `1px solid ${themeColor}44`,
                boxShadow: `0 20px 40px rgba(0,0,0,0.4), 0 0 20px ${themeBg}`,
                overflow: 'hidden',
                position: 'relative'
              }}
            >
              <Stack align="center" gap={0} p={{ base: 'lg', sm: 40 }} style={{ flex: 1, justifyContent: 'center' }}>
                <Badge variant="filled" size="sm" mb={30} style={{ backgroundColor: themeColor, color: '#000' }}>
                  {currentCard?.state === 0 ? 'NEW CARD' : 'REVIEW'}
                </Badge>

                <Title order={1} ta="center" style={{ fontSize: 'clamp(2rem, 8vw, 3.5rem)', color: '#fff', letterSpacing: '-0.5px', lineHeight: 1.2 }}>
                  {currentCard?.word}
                </Title>

                {currentCard?.sentence && (
                  <Text c="dimmed" ta="center" size="lg" mt="xl" fs="italic" style={{ maxWidth: '90%' }}>
                    "{currentCard.sentence}"
                  </Text>
                )}

                {!isFlipped && (
                  <Box mt={50} style={{ opacity: 0.4 }}>
                    <Group gap="xs" justify="center">
                      <IconBulb size={16} />
                      <Text size="xs" fw={700} style={{ letterSpacing: 2 }}>TAP TO REVEAL</Text>
                    </Group>
                  </Box>
                )}

                {isFlipped && (
                  <Box mt={40} pt={30} style={{ borderTop: '1px solid rgba(255,255,255,0.08)', width: '100%' }}>
                    <Group gap="xs" justify="center" mb="xs" opacity={0.5}>
                      <IconLamp size={14} />
                      <Text size="xs" fw={800} tt="uppercase">Definition / Note</Text>
                    </Group>
                    <Text ta="center" size="xl" fw={600} c="#e8eaf0" style={{ wordBreak: 'break-word' }}>
                      {currentCard?.note || "No notes provided."}
                    </Text>
                  </Box>
                )}
              </Stack>
            </Paper>
          )}
        </Transition>

        {isFlipped ? (
          <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="sm">
            {RATINGS.map(({ label, value, color, bg }, i) => (
              <Button
                key={label}
                variant="light"
                color="gray"
                size="xl"
                radius="md"
                onClick={() => handleRate(value)}
                styles={{
                  root: { backgroundColor: bg, border: `1px solid ${color}33`, height: 70 },
                  inner: { flexDirection: 'column', gap: 2 }
                }}
              >
                <Text fw={800} size="sm" style={{ color }}>{label}</Text>
                <Text size="xs" c="dimmed" fw={500}>[{i + 1}]</Text>
              </Button>
            ))}
          </SimpleGrid>
        ) : (
          <Button
            fullWidth size="xl" radius="md" onClick={() => setIsFlipped(true)}
            style={{ height: 70, fontSize: 18, background: 'linear-gradient(135deg, #1a4fc7, #2d7aff)', boxShadow: '0 8px 20px rgba(26, 79, 199, 0.3)' }}
          >
            SHOW ANSWER (Space)
          </Button>
        )}
      </Stack>
    </Container>
  );
}
