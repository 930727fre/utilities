import { useState, useMemo } from 'react';
import {
  Container, Textarea, Button, Title, Stack, Paper,
  ActionIcon, Group, Text, Alert, Code, Box, ThemeIcon, Badge, ScrollArea
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconArrowLeft, IconDatabaseImport, IconAlertCircle, IconCheck, IconFileText, IconAlertTriangle } from '@tabler/icons-react';
import { api } from '../api';
import { nanoid } from 'nanoid';
import { useNavigate } from 'react-router-dom';
import type { Card } from '../types';

interface ParsedLine {
  raw: string;
  word: string;
  note: string;
  sentence: string;
  malformed: boolean;
}

function parseLine(line: string): ParsedLine {
  const malformed = !line.includes('::');
  const [word = '', note = '', sentence = ''] = line.split('::').map(s => s.trim());
  return { raw: line, word: word || 'Untitled', note, sentence, malformed };
}

export default function BatchAddPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [content, setContent] = useState('');

  const parsedLines = useMemo<ParsedLine[]>(() => {
    if (!content.trim()) return [];
    return content.trim().split('\n').filter(l => l.trim() !== '').map(parseLine);
  }, [content]);

  const malformedCount = parsedLines.filter(l => l.malformed).length;
  const validCount = parsedLines.length;

  const handleBatchSubmit = async () => {
    if (!parsedLines.length) return;
    setLoading(true);

    const newCards: Card[] = parsedLines.map(({ word, note, sentence }) => ({
      id: nanoid(),
      word,
      sentence,
      note,
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
      reps: 0,
      learning_steps: 0,
    }));

    try {
      await api.batchAddCards(newCards);
      notifications.show({
        title: 'Import Successful',
        message: `Successfully imported ${newCards.length} cards`,
        color: 'green',
        icon: <IconCheck size={16} />,
      });
      navigate('/');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Unknown error';
      notifications.show({ title: 'Import Failed', message: msg, color: 'red' });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Box style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', backgroundColor: '#0a0c14' }}>
      <Container size="sm" maw={520} w="100%" py="xl" px="md">
        <Stack gap="lg">
          <Group justify="space-between">
            <Group gap="sm">
              <ActionIcon
                variant="subtle" onClick={() => navigate('/')} size="xl" radius="md" c="dimmed"
                style={{ border: '1px solid rgba(255,255,255,0.1)' }}
              >
                <IconArrowLeft size={24} />
              </ActionIcon>
              <Title order={2} c="#e8eaf0" style={{ letterSpacing: '-0.5px' }}>Batch Import</Title>
            </Group>
            <ThemeIcon variant="light" color="violet" size="lg" radius="md">
              <IconFileText size={20} />
            </ThemeIcon>
          </Group>

          <Alert
            variant="light" color="violet" radius="lg" icon={<IconAlertCircle size={20} />}
            styles={{
              root: { backgroundColor: 'rgba(121, 80, 242, 0.05)', border: '1px solid rgba(121, 80, 242, 0.2)' },
              title: { fontWeight: 700 },
            }}
          >
            <Text size="xs" c="dimmed" mb={4} fw={600}>Format (one per line):</Text>
            <Code block style={{ backgroundColor: 'rgba(0,0,0,0.3)', color: '#a5d8ff', fontSize: '11px', border: '1px solid rgba(255,255,255,0.05)' }}>
              word::note::sentence
            </Code>
          </Alert>

          <Paper
            radius={20} p="xl"
            style={{ background: 'linear-gradient(145deg, #161b2c 0%, #0d111d 100%)', border: '1px solid rgba(121, 80, 242, 0.3)', boxShadow: '0 20px 50px rgba(0,0,0,0.5)' }}
          >
            <Stack gap="md">
              <Textarea
                placeholder={"Apple::蘋果::An apple a day.\nBanana::香蕉::I like bananas."}
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
                    borderRadius: '12px',
                  },
                  label: { color: '#e8eaf0', marginBottom: '8px', fontWeight: 600 },
                }}
              />

              {/* Live parse preview */}
              {parsedLines.length > 0 && (
                <Box>
                  <Group justify="space-between" mb="xs">
                    <Group gap="xs">
                      <Badge color="violet" variant="light" size="sm">{validCount} cards</Badge>
                      {malformedCount > 0 && (
                        <Badge color="orange" variant="light" size="sm" leftSection={<IconAlertTriangle size={10} />}>
                          {malformedCount} malformed
                        </Badge>
                      )}
                    </Group>
                    <Text size="xs" c="dimmed">Separator: ::</Text>
                  </Group>

                  <ScrollArea h={Math.min(parsedLines.length * 44, 220)} type="auto">
                    <Stack gap={4}>
                      {parsedLines.map((line, i) => (
                        <Box
                          key={i}
                          px="sm" py={6}
                          style={{
                            borderRadius: 8,
                            background: line.malformed ? 'rgba(255, 140, 0, 0.08)' : 'rgba(255,255,255,0.03)',
                            border: `1px solid ${line.malformed ? 'rgba(255,140,0,0.25)' : 'rgba(255,255,255,0.05)'}`,
                          }}
                        >
                          {line.malformed ? (
                            <Group gap="xs" wrap="nowrap">
                              <IconAlertTriangle size={13} color="#ff8c00" style={{ flexShrink: 0 }} />
                              <Text size="xs" c="orange.4" style={{ fontFamily: 'JetBrains Mono', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {line.raw}
                              </Text>
                              <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>— missing ::</Text>
                            </Group>
                          ) : (
                            <Group gap="xs" wrap="nowrap">
                              <Text size="xs" fw={700} c="#e8eaf0" style={{ minWidth: 80, maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {line.word}
                              </Text>
                              {line.note && (
                                <Text size="xs" c="dimmed" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                                  {line.note}
                                </Text>
                              )}
                              {line.sentence && (
                                <Text size="xs" c="dimmed" fs="italic" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                                  "{line.sentence}"
                                </Text>
                              )}
                            </Group>
                          )}
                        </Box>
                      ))}
                    </Stack>
                  </ScrollArea>
                </Box>
              )}

              <Button
                fullWidth size="xl" radius="md"
                leftSection={<IconDatabaseImport size={20} />}
                onClick={handleBatchSubmit}
                loading={loading}
                disabled={!parsedLines.length}
                style={{
                  background: 'linear-gradient(135deg, #5f3dc4, #7950f2)',
                  boxShadow: '0 8px 20px rgba(95, 61, 196, 0.3)',
                  border: 'none', height: 56, color: '#fff', fontWeight: 700,
                }}
              >
                Import {validCount > 0 ? `${validCount} Cards` : ''}
              </Button>
            </Stack>
          </Paper>
        </Stack>
      </Container>
    </Box>
  );
}
