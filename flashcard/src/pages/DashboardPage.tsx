import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Container, Title, Text, Paper, Group, Stack,
  Button, ThemeIcon, Skeleton, Box, Badge, Center, Progress
} from '@mantine/core';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import {
  IconFlame, IconCards, IconPlus,
  IconPlayerPlay, IconUpload, IconList, IconCheck
} from '@tabler/icons-react';

const PHASE_CONFIG = {
  done: {
    borderColor: '#1e5c36', bg: '#0e1f16',
    iconColor: 'green', tagColor: '#4dbb7a', tag: 'All Clear',
    icon: <IconCheck size={22} />,
    badge: { color: '#1a3d28', text: '#4dbb7a', border: '#1e5c36', label: 'All Clear' },
    button: { gradient: { from: '#1a3d28', to: '#2a5c3e' }, label: 'All done for today!' },
    disabled: true,
  },
  review: {
    borderColor: '#1f3e70', bg: '#101a2e',
    iconColor: 'blue', tagColor: '#4a8fff', tag: 'Current Task: Review',
    icon: <IconCards size={22} />,
    badge: { color: '#122040', text: '#4a8fff', border: '#1f3e70', label: 'Phase 1' },
    button: { gradient: { from: '#1a4fc7', to: '#2d7aff' }, label: null },
    disabled: false,
  },
  learning: {
    borderColor: '#7a5e10', bg: '#22190a',
    iconColor: 'yellow', tagColor: '#f5c542', tag: "Today's New Cards",
    icon: <IconPlus size={22} />,
    badge: { color: '#2a2008', text: '#f5c542', border: '#7a5e10', label: 'Phase 2' },
    button: { gradient: { from: '#b87b00', to: '#f0a500' }, label: null },
    disabled: false,
  },
} as const;

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
      <Center h="100vh" bg="#0a0c14">
        <Container size="sm" maw={480} w="100%" px="md">
          <Stack gap="md">
            <Skeleton height={120} radius={14} animate />
            <Skeleton height={90} radius={14} animate />
            <Skeleton height={56} radius={14} animate />
            <Group grow gap="sm">
              <Skeleton height={48} radius={8} animate />
              <Skeleton height={48} radius={8} animate />
            </Group>
            <Center mt="md">
              <Stack gap={4} align="center">
                <Text c="dimmed" size="xs" fw={700} style={{ letterSpacing: '1.5px' }}>
                  SYNCING WITH BACKEND
                </Text>
                <Progress value={100} w={120} size="xs" radius="xl" animated color="blue" />
              </Stack>
            </Center>
          </Stack>
        </Container>
      </Center>
    );
  }

  const queueSize = stats.due_count > 0 ? stats.due_count : stats.new_available;
  const phase = stats.due_count > 0 ? 'review' : queueSize > 0 ? 'learning' : 'done';
  const cfg = PHASE_CONFIG[phase];

  const phaseTitle = phase === 'done'
    ? <Text fw={600} size="lg" c="#e8eaf0">No tasks for today 🎉</Text>
    : phase === 'review'
    ? <Text fw={600} size="lg" c="#e8eaf0"><span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 20 }}>{queueSize}</span> cards to review</Text>
    : <Text fw={600} size="lg" c="#e8eaf0"><span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 20 }}>{queueSize}</span> new cards to learn</Text>;

  const buttonLabel = cfg.button.label ?? `Start ${phase === 'learning' ? 'Learning' : 'Review'} (${queueSize} cards)`;

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      alignItems: 'center',
      backgroundColor: '#0a0c14'
    }}>
      <Container size="sm" maw={480} px="md" w="100%" style={{ paddingBottom: 'max(24px, env(safe-area-inset-bottom))' }}>
        <Stack gap="md">

          {/* 連勝卡片 */}
          <Paper radius={14} p="xl"
            style={{
              background: 'linear-gradient(135deg, #2a1f14 0%, #181c27 100%)',
              border: '1px solid #a85a2a',
              boxShadow: '0 10px 30px rgba(0,0,0,0.3)'
            }}>
            <Group gap="xl" wrap="nowrap">
              <IconFlame size={52} color="#ff9151" fill="#ff6b1a" style={{ flexShrink: 0 }} />
              <Box>
                <Text size="xs" fw={600} c="#ff9151" tt="uppercase" style={{ letterSpacing: '1.5px' }}>
                  Current Streak
                </Text>
                <Title order={1} style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 38, color: '#ffb27a', lineHeight: 1.1 }}>
                  {stats.streak_count}{' '}
                  <Text span size="lg" c="#ff9151" fw={400}>Days</Text>
                </Title>
              </Box>
            </Group>
          </Paper>

          {/* 狀態卡片 */}
          <Paper radius={14} p="lg"
            style={{
              background: cfg.bg,
              border: `1px solid ${cfg.borderColor}`,
              boxShadow: '0 10px 25px rgba(0,0,0,0.2)'
            }}>
            <Group justify="space-between" wrap="nowrap" gap="sm">
              <Group gap="md" wrap="nowrap" style={{ minWidth: 0 }}>
                <ThemeIcon size={44} radius={10} color={cfg.iconColor} variant="light" style={{ flexShrink: 0 }}>
                  {cfg.icon}
                </ThemeIcon>
                <Box style={{ minWidth: 0, overflow: 'hidden' }}>
                  <Text size="xs" fw={600} tt="uppercase" style={{ letterSpacing: '1.2px', color: cfg.tagColor, marginBottom: 3 }}>
                    {cfg.tag}
                  </Text>
                  {phaseTitle}
                </Box>
              </Group>
              <Badge style={{
                background: cfg.badge.color,
                color: cfg.badge.text,
                border: `1px solid ${cfg.badge.border}`,
                flexShrink: 0
              }}>
                {cfg.badge.label}
              </Badge>
            </Group>
          </Paper>

          {/* 主按鈕 */}
          <Button
            size="lg"
            radius={14}
            disabled={cfg.disabled}
            onClick={() => !cfg.disabled && navigate('/review')}
            leftSection={<IconPlayerPlay size={20} fill="currentColor" />}
            style={{
              background: cfg.disabled
                ? '#1e2335'
                : `linear-gradient(135deg, ${cfg.button.gradient.from}, ${cfg.button.gradient.to})`,
              color: phase === 'learning' ? '#1a1200' : '#fff',
              border: 'none',
              height: 54,
              fontSize: 16,
              fontWeight: 600,
              boxShadow: cfg.disabled ? 'none' : '0 8px 20px rgba(0,0,0,0.3)'
            }}
          >
            {buttonLabel}
          </Button>

          {/* 功能按鈕 */}
          <Group grow gap="sm">
            <Button
              variant="outline"
              size="md"
              radius={8}
              leftSection={<IconUpload size={18} />}
              onClick={() => navigate('/batch-add')}
              style={{ borderColor: '#2a2f45', color: '#e8eaf0', height: 48 }}
            >
              Batch Import
            </Button>
            <Button
              variant="outline"
              size="md"
              radius={8}
              leftSection={<IconList size={18} />}
              onClick={() => navigate('/edit')}
              style={{ borderColor: '#2a2f45', color: '#e8eaf0', height: 48 }}
            >
              Edit
            </Button>
          </Group>

        </Stack>
      </Container>
    </div>
  );
}
