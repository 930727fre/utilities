import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Stack, Title, Text, Button, Group, Box,
} from '@mantine/core';
import { useNavigate } from 'react-router-dom';
import { IconArrowLeft } from '@tabler/icons-react';
import { api } from '../api';
import PageShell from '../components/PageShell';
import CardShell from '../components/CardShell';
import SwipeCard from '../components/SwipeCard';

export default function DrillPage() {
  const navigate = useNavigate();
  const { data: cards } = useQuery({
    queryKey: ['today-drill'],
    queryFn: api.getTodayDrill,
  });

  const [index, setIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);

  const total = cards?.length ?? 0;
  const current = cards?.[index];

  const next = () => {
    if (index < total - 1) {
      setIndex((i) => i + 1);
      setFlipped(false);
    }
  };
  const prev = () => {
    if (index > 0) {
      setIndex((i) => i - 1);
      setFlipped(false);
    }
  };

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.code === 'Space' || e.code === 'Enter') {
        e.preventDefault();
        setFlipped((f) => !f);
      } else if (e.key === 'ArrowRight') {
        next();
      } else if (e.key === 'ArrowLeft') {
        prev();
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, total]);

  return (
    <PageShell scroll="locked">
      <Stack gap="md" style={{ flex: 1, minHeight: 0 }}>
        <Group justify="space-between">
          <Button
            variant="subtle"
            leftSection={<IconArrowLeft size={16} />}
            onClick={() => navigate('/')}
            style={{ color: 'var(--text)', fontFamily: 'var(--mono)' }}
          >
            Back
          </Button>
          {cards && (
            <Text c="var(--text-dim)" size="xs" style={{ fontFamily: 'var(--mono)' }}>
              {total === 0 ? '0 / 0' : `${index + 1} / ${total}`}
            </Text>
          )}
        </Group>

        {!cards && (
          <CardShell>
            <Box p="lg"><Text c="var(--text-dim)">Loading...</Text></Box>
          </CardShell>
        )}

        {cards && cards.length === 0 && (
          <CardShell>
            <Stack align="center" justify="center" p={{ base: 'lg', sm: 40 }} style={{ flex: 1 }}>
              <Text c="var(--text-dim)" size="xs"
                style={{ fontFamily: 'var(--mono)', letterSpacing: 2, textTransform: 'uppercase' }}>
                no drill today
              </Text>
              <Title order={2} c="var(--text-h)" ff="var(--mono)">
                ✓ Done
              </Title>
            </Stack>
          </CardShell>
        )}

        {current && (
          <>
            <SwipeCard
              onLeft={prev}
              onRight={next}
              disabled={index === 0 && index === total - 1}
              leftHint="rgba(170, 170, 170, 0.15)"
              rightHint="rgba(170, 170, 170, 0.15)"
            >
              <CardShell onClick={() => setFlipped((f) => !f)}>
                <Stack gap="md" p={{ base: 'lg', sm: 40 }} style={{ flex: 1, justifyContent: 'center' }}>
                  <Text c="var(--text-h)" ta="center"
                    style={{ fontSize: 'clamp(1.1rem, 4vw, 1.5rem)', lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>
                    {current.prompt}
                  </Text>

                  {flipped ? (
                    <Box mt="xl" pt="lg" style={{ borderTop: '1px solid var(--border)' }}>
                      <Text c="var(--text-h)" ta="center" fw={600}
                        style={{ fontSize: 'clamp(1rem, 3.5vw, 1.4rem)', lineHeight: 1.5 }}>
                        {current.answer}
                      </Text>
                      {current.source_error_id && (
                        <Text c="var(--text-dim)" size="xs" ta="center" mt="md"
                          style={{ fontFamily: 'var(--mono)' }}>
                          errors.md: {current.source_error_id}
                        </Text>
                      )}
                    </Box>
                  ) : (
                    <Text c="var(--text-dim)" size="xs" ta="center" mt="xl"
                      style={{ fontFamily: 'var(--mono)', letterSpacing: 1, textTransform: 'uppercase' }}>
                      tap to reveal
                    </Text>
                  )}
                </Stack>
              </CardShell>
            </SwipeCard>

            <Text c="var(--text-dim)" size="xs" ta="center"
              style={{ fontFamily: 'var(--mono)' }}>
              tap to flip · swipe to navigate
            </Text>
          </>
        )}
      </Stack>
    </PageShell>
  );
}
