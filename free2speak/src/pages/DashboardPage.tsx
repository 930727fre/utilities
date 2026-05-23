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

            {/* Actions — parallel, equal-weight */}
            <Group grow gap="sm">
              <Button
                size="lg"
                radius={8}
                onClick={() => navigate('/practice')}
                style={{
                  background: 'transparent',
                  color: 'var(--text-h)',
                  border: '1px solid var(--border)',
                  height: 54,
                  fontFamily: 'var(--mono)',
                }}
              >
                Practice
              </Button>
              <Button
                size="lg"
                radius={8}
                onClick={() => navigate('/drill')}
                style={{
                  background: 'transparent',
                  color: 'var(--text-h)',
                  border: '1px solid var(--border)',
                  height: 54,
                  fontFamily: 'var(--mono)',
                }}
              >
                Drill
              </Button>
            </Group>

            {/* Footer stat */}
            <Text size="xs" c="var(--text-dim)" ta="center" mt="sm"
              style={{ fontFamily: 'var(--mono)' }}>
              {stats?.active_errors_count ?? 0} active errors
            </Text>

          </Stack>
        )}
      </Transition>
    </PageShell>
  );
}
