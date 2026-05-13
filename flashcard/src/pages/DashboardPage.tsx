import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Title, Text, Paper, Group, Stack,
  Button, Skeleton, Box, Progress
} from '@mantine/core';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import {
  IconFlame, IconCards, IconPlus,
  IconPlayerPlay, IconUpload, IconPencil, IconCheck
} from '@tabler/icons-react';
import PageShell from '../components/PageShell';

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace';

export default function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: stats } = useQuery({
    queryKey: ['stats'],
    queryFn: api.getStats,
    staleTime: 30_000,
  });

  useEffect(() => {
    queryClient.prefetchQuery({ queryKey: ['queue'], queryFn: api.getQueue, staleTime: 60_000 });
  }, [queryClient]);

  if (!stats) {
    return (
      <PageShell scroll="centered" maw={480}>
        <Stack gap="md">
          <Skeleton height={120} radius={14} animate />
          <Skeleton height={90} radius={14} animate />
          <Skeleton height={56} radius={14} animate />
          <Group grow gap="sm">
            <Skeleton height={48} radius={8} animate />
            <Skeleton height={48} radius={8} animate />
          </Group>
          <Stack gap={4} align="center" mt="md">
            <Text c="#aeaeb2" size="xs" fw={700} style={{ letterSpacing: '1.5px', fontFamily: MONO }}>
              SYNCING WITH BACKEND
            </Text>
            <Progress value={100} w={120} size="xs" radius="xl" animated color="gray" />
          </Stack>
        </Stack>
      </PageShell>
    );
  }

  const queueSize = stats.due_count > 0 ? stats.due_count : stats.new_available;
  const phase = stats.due_count > 0 ? 'review' : queueSize > 0 ? 'learning' : 'done';
  const isDone = phase === 'done';

  const phaseIcon = isDone
    ? <IconCheck size={52} color="#aeaeb2" style={{ flexShrink: 0 }} />
    : phase === 'review'
    ? <IconCards size={52} color="#aeaeb2" style={{ flexShrink: 0 }} />
    : <IconPlus size={52} color="#aeaeb2" style={{ flexShrink: 0 }} />;

  const phaseTag = isDone ? 'All Clear' : phase === 'review' ? 'Due Today' : 'New Cards';

  const ctaLabel = isDone
    ? 'All done for today'
    : `Start ${phase === 'learning' ? 'Learning' : 'Review'} (${queueSize} cards)`;

  return (
    <PageShell scroll="centered" maw={480}>
      <Stack gap="md">

          {/* Streak */}
          <Paper radius={14} p="xl"
            style={{
              background: '#2c2c2e',
              border: '1px solid #3a3a3c',
              boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
            }}>
            <Group gap="xl" wrap="nowrap">
              <IconFlame size={52} color="#aeaeb2" style={{ flexShrink: 0 }} />
              <Box>
                <Text size="xs" fw={600} c="#aeaeb2" tt="uppercase" style={{ letterSpacing: '1.5px', fontFamily: MONO }}>
                  Current Streak
                </Text>
                <Title order={1} style={{ fontFamily: MONO, fontSize: 38, color: '#e8e3d9', lineHeight: 1.1 }}>
                  {stats.streak_count}{' '}
                  <Text span size="lg" c="#aeaeb2" fw={400}>Days</Text>
                </Title>
              </Box>
            </Group>
          </Paper>

          {/* Phase */}
          <Paper radius={14} p="xl"
            style={{
              background: '#2c2c2e',
              border: '1px solid #3a3a3c',
              boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
            }}>
            <Group gap="xl" wrap="nowrap">
              {phaseIcon}
              <Box>
                <Text size="xs" fw={600} c="#aeaeb2" tt="uppercase" style={{ letterSpacing: '1.5px', fontFamily: MONO }}>
                  {phaseTag}
                </Text>
                {isDone ? (
                  <Title order={2} style={{ fontFamily: MONO, fontSize: 38, color: '#aeaeb2', lineHeight: 1.1 }}>
                    Done
                  </Title>
                ) : (
                  <Title order={2} style={{ fontFamily: MONO, fontSize: 38, color: '#e8e3d9', lineHeight: 1.1 }}>
                    {queueSize}{' '}
                    <Text span size="lg" c="#aeaeb2" fw={400}>cards</Text>
                  </Title>
                )}
              </Box>
            </Group>
          </Paper>

          {/* Actions */}
          <Group grow gap="sm">
            <Button
              size="lg"
              radius={8}
              disabled={isDone}
              onClick={() => !isDone && navigate('/review')}
              title={ctaLabel}
              aria-label={ctaLabel}
              style={{
                background: isDone ? '#2c2c2e' : '#c79968',
                color: isDone ? '#636366' : '#1c1c1e',
                border: isDone ? '1px solid #3a3a3c' : 'none',
                height: 54,
              }}
            >
              <IconPlayerPlay size={24} fill="currentColor" />
            </Button>
            <Button
              variant="outline"
              size="lg"
              radius={8}
              onClick={() => navigate('/batch-add')}
              title="Batch Import"
              aria-label="Batch Import"
              style={{ borderColor: '#3a3a3c', color: '#e8e3d9', height: 54, background: 'transparent' }}
            >
              <IconUpload size={22} />
            </Button>
            <Button
              variant="outline"
              size="lg"
              radius={8}
              onClick={() => navigate('/edit')}
              title="Edit"
              aria-label="Edit"
              style={{ borderColor: '#3a3a3c', color: '#e8e3d9', height: 54, background: 'transparent' }}
            >
              <IconPencil size={22} />
            </Button>
          </Group>

      </Stack>
    </PageShell>
  );
}
