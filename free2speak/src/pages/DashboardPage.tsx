import { useQuery } from '@tanstack/react-query';
import {
  Title, Text, Paper, Group, Stack,
  Button, Box, Transition,
} from '@mantine/core';
import { useNavigate } from 'react-router-dom';
import {
  IconFlame, IconMicrophone, IconCards, IconCheck,
} from '@tabler/icons-react';
import { api } from '../api';
import PageShell from '../components/PageShell';

export default function DashboardPage() {
  const navigate = useNavigate();
  const { data: stats, isStale, isFetching } = useQuery({
    queryKey: ['today-stats'],
    queryFn: api.getTodayStats,
    staleTime: 30_000,
  });

  // Prefetch Practice's roleplay and Drill's cards in the background while the
  // user looks at stats. If either needs an Opus generation (~10s), it happens
  // silently here rather than blocking the page they navigate to next. Same
  // staleTime as the consuming pages so the cache survives the hop.
  const roleplayPrefetch = useQuery({
    queryKey: ['today-roleplay'],
    queryFn: api.getTodayRoleplay,
    staleTime: 5 * 60 * 1000,
  });
  const drillPrefetch = useQuery({
    queryKey: ['today-drill'],
    queryFn: api.getTodayDrill,
    staleTime: 5 * 60 * 1000,
  });
  const practiceReady = !!roleplayPrefetch.data;
  const drillReady = !!drillPrefetch.data;

  const isReady = !!stats && !(isStale && isFetching);

  const doneCount =
    (stats?.practice_done_today ? 1 : 0) + (stats?.drill_done_today ? 1 : 0);
  const allDone = doneCount === 2;

  const statusIcon = allDone
    ? <IconCheck size={52} color="var(--text)" style={{ flexShrink: 0 }} />
    : !stats?.practice_done_today
    ? <IconMicrophone size={52} color="var(--text)" style={{ flexShrink: 0 }} />
    : <IconCards size={52} color="var(--text)" style={{ flexShrink: 0 }} />;

  const statusTag = allDone ? 'All Clear' : 'Today';

  return (
    <PageShell scroll="centered" maw={480}>
      <Transition mounted={isReady} transition="slide-up" duration={400}>
        {(styles) => (
          <Stack gap="md" style={styles}>

            {/* Streak */}
            <Paper radius={14} p="xl"
              style={{
                background: 'var(--card)',
                border: '1px solid var(--border)',
                boxShadow: 'var(--shadow)',
              }}>
              <Group gap="xl" wrap="nowrap">
                <IconFlame size={52} color="var(--text)" style={{ flexShrink: 0 }} />
                <Box>
                  <Text size="xs" fw={600} c="var(--text)" tt="uppercase"
                    style={{ letterSpacing: '1.5px', fontFamily: 'var(--mono)' }}>
                    Current Streak
                  </Text>
                  <Title order={1}
                    style={{ fontFamily: 'var(--mono)', fontSize: 38, color: 'var(--text-h)', lineHeight: 1.1 }}>
                    {stats?.streak_count}{' '}
                    <Text span size="lg" c="var(--text)" fw={400}>Days</Text>
                  </Title>
                </Box>
              </Group>
            </Paper>

            {/* Today's status */}
            <Paper radius={14} p="xl"
              style={{
                background: 'var(--card)',
                border: '1px solid var(--border)',
                boxShadow: 'var(--shadow)',
              }}>
              <Group gap="xl" wrap="nowrap">
                {statusIcon}
                <Box>
                  <Text size="xs" fw={600} c="var(--text)" tt="uppercase"
                    style={{ letterSpacing: '1.5px', fontFamily: 'var(--mono)' }}>
                    {statusTag}
                  </Text>
                  <Title order={2}
                    style={{
                      fontFamily: 'var(--mono)',
                      fontSize: 38,
                      color: allDone ? 'var(--text)' : 'var(--text-h)',
                      lineHeight: 1.1,
                    }}>
                    {doneCount}{' '}
                    <Text span size="lg" c="var(--text)" fw={400}>/ 2 done</Text>
                  </Title>
                </Box>
              </Group>
            </Paper>

            {/* Actions — icon-only (label conveyed by icon + aria-label).
                Disabled until prefetch resolves so clicking always lands on a
                fully-loaded page rather than a spinner. */}
            <Group grow gap="sm">
              <Button
                size="lg"
                radius={8}
                disabled={!practiceReady}
                onClick={() => navigate('/practice')}
                title="Practice"
                aria-label="Practice"
                style={{
                  background: 'transparent',
                  color: 'var(--text-h)',
                  border: '1px solid var(--border)',
                  height: 54,
                  opacity: practiceReady ? 1 : 0.45,
                }}
              >
                <IconMicrophone size={24} />
              </Button>
              <Button
                size="lg"
                radius={8}
                disabled={!drillReady}
                onClick={() => navigate('/drill')}
                title="Drill"
                aria-label="Drill"
                style={{
                  background: 'transparent',
                  color: 'var(--text-h)',
                  border: '1px solid var(--border)',
                  height: 54,
                  opacity: drillReady ? 1 : 0.45,
                }}
              >
                <IconCards size={24} />
              </Button>
            </Group>

          </Stack>
        )}
      </Transition>
    </PageShell>
  );
}
