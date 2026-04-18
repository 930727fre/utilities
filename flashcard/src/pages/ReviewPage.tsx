import { useState, useEffect, useCallback, useRef } from 'react';
import {
  Container, Paper, Title, Text, Button, Group, Stack,
  Progress, ActionIcon, ThemeIcon, SimpleGrid, Center, Box, Badge, Transition
} from '@mantine/core';
import { IconArrowLeft, IconCheck, IconLamp, IconBulb } from '@tabler/icons-react';
import { useNavigate } from 'react-router-dom';
import { computeNext } from '../lib/fsrs';
import { generatorParameters, Rating } from 'ts-fsrs';
import { api } from '../api';
import type { Card } from '../types';

export default function ReviewPage() {
  const navigate = useNavigate();

  const [reviewQueue, setReviewQueue] = useState<Card[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [isFlipped, setIsFlipped] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const fsrsParamsRef = useRef<object>(generatorParameters());
  const newCountRef = useRef(0);

  useEffect(() => {
    api.getQueue().then(({ cards, daily_new_count, fsrs_params }) => {
      if (fsrs_params && fsrs_params !== 'undefined') {
        try { fsrsParamsRef.current = JSON.parse(fsrs_params); } catch { /* use default */ }
      }
      newCountRef.current = Number(daily_new_count || 0);
      setReviewQueue(cards);
      setTotalCount(cards.length);
      setIsLoading(false);
    }).catch(console.error);
  }, []);

  const currentCard = reviewQueue[0];
  const reviewed = totalCount - reviewQueue.length;

  const handleRate = useCallback((rating: Rating) => {
    if (!currentCard) return;

    const isNew = Number(currentCard.state) === 0;
    const nextFields = computeNext(currentCard, rating, fsrsParamsRef.current as never);

    console.log('[card update]', currentCard.id, nextFields);
    api.updateCard(currentCard.id, nextFields).catch(console.error);

    if (isNew) {
      newCountRef.current += 1;
      console.log('[settings update] daily_new_count', newCountRef.current);
      api.updateSettings({ daily_new_count: String(newCountRef.current) }).catch(console.error);
    }

    setReviewQueue(prev => prev.slice(1));
    setIsFlipped(false);
  }, [currentCard]);

  useEffect(() => {
    if (!currentCard) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.code === 'Space' || e.code === 'Enter') {
        e.preventDefault();
        setIsFlipped(true);
      } else if (isFlipped) {
        const keyMap: Record<string, Rating> = {
          '1': Rating.Again, '2': Rating.Hard, '3': Rating.Good, '4': Rating.Easy
        };
        if (keyMap[e.key]) handleRate(keyMap[e.key]);
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
                <Title order={2} c="#e8eaf0">今日任務已完成！</Title>
                <Text c="dimmed" mt="sm">做得好！所有的卡片都已經複習完畢。🔥</Text>
              </Box>
              <Button
                size="lg"
                radius="md"
                fullWidth
                onClick={() => navigate('/')}
                style={{ background: 'linear-gradient(135deg, #1a3d28, #2a5c3e)', border: 'none' }}
              >
                回到儀表板
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
    <Container size="sm" py="xl" px="md">
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
              <Stack align="center" gap={0} p={40} style={{ flex: 1, justifyContent: 'center' }}>
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
                    <Text ta="center" size="xl" fw={600} c="#e8eaf0">
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
            {([
              { label: 'Again', color: '#ff6b6b', rating: Rating.Again, bg: 'rgba(255,107,107,0.1)' },
              { label: 'Hard',  color: '#ffd43b', rating: Rating.Hard,  bg: 'rgba(255,212,59,0.1)'  },
              { label: 'Good',  color: '#51cf66', rating: Rating.Good,  bg: 'rgba(81,207,102,0.1)'  },
              { label: 'Easy',  color: '#339af0', rating: Rating.Easy,  bg: 'rgba(51,154,240,0.1)'  },
            ] as const).map(({ label, color, rating, bg }, i) => (
              <Button
                key={label}
                variant="light"
                color="gray"
                size="xl"
                radius="md"
                onClick={() => handleRate(rating)}
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
