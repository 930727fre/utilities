import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Stack, Title, Text, Group, Box, Button,
} from '@mantine/core';
import { IconChevronLeft, IconChevronRight } from '@tabler/icons-react';
import { api } from '../api';
import PageShell from '../components/PageShell';
import CardShell from '../components/CardShell';

export default function DrillPage() {
  const { data: cards, isLoading, isFetching } = useQuery({
    queryKey: ['today-drill'],
    queryFn: api.getTodayDrill,
  });
  // Show indicator when a background refetch is happening but cached cards are visible
  const refreshing = isFetching && !isLoading;

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
        {refreshing && (
          <Text c="var(--text)" className="glyph-pulse"
            style={{ fontFamily: 'var(--mono)', fontSize: 13, textAlign: 'center' }}>
            ○ regenerating drill…
          </Text>
        )}

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
            <CardShell onClick={() => setFlipped((f) => !f)}>
              <Stack gap="md" p={{ base: 'lg', sm: 40 }}
                style={{ flex: 1, justifyContent: 'center', position: 'relative' }}>
                <Text c="var(--text-dim)" size="xs"
                  style={{
                    position: 'absolute', top: 16, right: 20,
                    fontFamily: 'var(--mono)',
                  }}>
                  {index + 1} / {total}
                </Text>
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

            <Group grow gap="sm">
              <Button
                size="lg"
                radius={8}
                disabled={index === 0}
                onClick={prev}
                leftSection={<IconChevronLeft size={18} />}
                style={{
                  background: 'transparent',
                  color: 'var(--text-h)',
                  border: '1px solid var(--border)',
                  height: 54,
                  fontFamily: 'var(--mono)',
                }}
              >
                Prev
              </Button>
              <Button
                size="lg"
                radius={8}
                disabled={index === total - 1}
                onClick={next}
                rightSection={<IconChevronRight size={18} />}
                style={{
                  background: 'transparent',
                  color: 'var(--text-h)',
                  border: '1px solid var(--border)',
                  height: 54,
                  fontFamily: 'var(--mono)',
                }}
              >
                Next
              </Button>
            </Group>
          </>
        )}
      </Stack>
    </PageShell>
  );
}
