import { useState } from 'react';
import {
  Container, Textarea, Button, Title, Stack, Paper,
  ActionIcon, Group, Text, Alert, Code, Box, Transition, ThemeIcon
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconArrowLeft, IconDatabaseImport, IconAlertCircle, IconCheck, IconFileText } from '@tabler/icons-react';
import { api } from '../api';
import { nanoid } from 'nanoid';
import { useNavigate } from 'react-router-dom';
import type { Card } from '../types';

export default function BatchAddPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [content, setContent] = useState('');

  const handleBatchSubmit = async () => {
    if (!content.trim()) return;
    setLoading(true);

    const lines = content.trim().split('\n');

    const newCards: Card[] = lines
      .filter(line => line.trim() !== '')
      .map(line => {
        const [word, note, sentence] = line.split('::').map(s => s.trim());
        return {
          id: nanoid(),
          word: word || 'Untitled',
          sentence: sentence || '',
          note: note || '',
          due: '',
          stability: 0,
          difficulty: 0,
          elapsed_days: 0,
          scheduled_days: 0,
          lapses: 0,
          state: 0,
          last_review: '',
          lang: 'en',
          created_at: new Date().toISOString(),
        };
      });

    try {
      await api.batchAddCards(newCards);

      notifications.show({
        title: '匯入成功',
        message: `已成功匯入 ${newCards.length} 張卡片`,
        color: 'green',
        icon: <IconCheck size={16} />
      });

      navigate('/');
    } catch {
      notifications.show({ title: '匯入失敗', message: '請檢查網路連線', color: 'red' });
    } finally {
      setLoading(false);
    }
  };

  const lineCount = content.trim() ? content.trim().split('\n').length : 0;

  return (
    <Box
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
        backgroundColor: '#0a0c14',
      }}
    >
      <Container size="sm" maw={520} w="100%" py="xl" px="md">
        <Transition mounted={true} transition="slide-up" duration={400}>
          {(styles) => (
            <div style={styles}>
              <Stack gap="lg">
                {/* 頂部標題 */}
                <Group justify="space-between">
                  <Group gap="sm">
                    <ActionIcon
                      variant="subtle"
                      onClick={() => navigate('/')}
                      size="xl"
                      radius="md"
                      c="dimmed"
                      style={{ border: '1px solid rgba(255,255,255,0.1)' }}
                    >
                      <IconArrowLeft size={24} />
                    </ActionIcon>
                    <Title order={2} c="#e8eaf0" style={{ letterSpacing: '-0.5px' }}>批量匯入</Title>
                  </Group>
                  <ThemeIcon variant="light" color="violet" size="lg" radius="md">
                    <IconFileText size={20} />
                  </ThemeIcon>
                </Group>

                {/* 格式提示 */}
                <Alert
                  variant="light"
                  color="violet"
                  radius="lg"
                  icon={<IconAlertCircle size={20} />}
                  styles={{
                    root: { backgroundColor: 'rgba(121, 80, 242, 0.05)', border: '1px solid rgba(121, 80, 242, 0.2)' },
                    title: { fontWeight: 700 }
                  }}
                >
                  <Text size="xs" c="dimmed" mb={4} fw={600}>格式說明 (每行一筆)：</Text>
                  <Code
                    block
                    style={{
                      backgroundColor: 'rgba(0,0,0,0.3)',
                      color: '#a5d8ff',
                      fontSize: '11px',
                      border: '1px solid rgba(255,255,255,0.05)'
                    }}
                  >
                    單字::筆記::例句
                  </Code>
                </Alert>

                {/* 輸入區塊 */}
                <Paper
                  radius={20}
                  p="xl"
                  style={{
                    background: 'linear-gradient(145deg, #161b2c 0%, #0d111d 100%)',
                    border: '1px solid rgba(121, 80, 242, 0.3)',
                    boxShadow: '0 20px 50px rgba(0,0,0,0.5)'
                  }}
                >
                  <Stack gap="md">
                    <Textarea
                      placeholder="Apple::蘋果::An apple a day.&#10;Banana::香蕉::I like bananas."
                      minRows={12}
                      autosize
                      value={content}
                      onChange={(e) => setContent(e.target.value)}
                      styles={{
                        input: {
                          fontFamily: 'JetBrains Mono, monospace',
                          backgroundColor: 'rgba(0,0,0,0.2)',
                          color: '#e8eaf0',
                          border: '1px solid rgba(255,255,255,0.05)',
                          padding: '16px',
                          fontSize: '14px',
                          borderRadius: '12px'
                        },
                        label: { color: '#e8eaf0', marginBottom: '8px', fontWeight: 600 }
                      }}
                    />

                    <Box mt="xs">
                      <Group justify="space-between" mb="lg">
                        <Text size="xs" c="dimmed" fw={700} tt="uppercase" style={{ letterSpacing: 1 }}>
                          Total Cards: <span style={{ color: '#7950f2' }}>{lineCount}</span>
                        </Text>
                        <Text size="xs" c="dimmed">Separator: ::</Text>
                      </Group>

                      <Button
                        fullWidth
                        size="xl"
                        radius="md"
                        leftSection={<IconDatabaseImport size={20} />}
                        onClick={handleBatchSubmit}
                        loading={loading}
                        disabled={!content.trim()}
                        style={{
                          background: 'linear-gradient(135deg, #5f3dc4, #7950f2)',
                          boxShadow: '0 8px 20px rgba(95, 61, 196, 0.3)',
                          border: 'none',
                          height: 56,
                          color: '#fff',
                          fontWeight: 700
                        }}
                      >
                        開始匯入資料庫
                      </Button>
                    </Box>
                  </Stack>
                </Paper>
              </Stack>
            </div>
          )}
        </Transition>
      </Container>
    </Box>
  );
}
