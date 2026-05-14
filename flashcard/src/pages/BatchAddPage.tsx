import { useState, useMemo } from 'react';
import { Textarea, Button, Title, Stack, Group, Text, Box } from '@mantine/core';
import PageShell from '../components/PageShell';
import { notifications } from '@mantine/notifications';
import { api } from '../api';
import { nanoid } from 'nanoid';
import { useNavigate } from 'react-router-dom';
import type { Card } from '../types';

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace';

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
      });
      navigate('/');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Unknown error';
      notifications.show({ title: 'Import Failed', message: msg });
    } finally {
      setLoading(false);
    }
  };

  return (
    <PageShell maw={520}>
      <Stack gap="lg">
        <Group gap="md" align="center">
          <Text
            c="#e8e3d9"
            style={{ cursor: 'pointer', fontFamily: MONO, fontSize: 26, lineHeight: 1 }}
            onClick={() => navigate('/')}
            title="Back"
            aria-label="Back"
            role="button"
          >
            ←
          </Text>
          <Title order={2} c="#e8e3d9" style={{ letterSpacing: '-0.5px' }}>Batch Import</Title>
        </Group>

        <Textarea
          placeholder={"Apple::蘋果::An apple a day.\nBanana::香蕉::I like bananas."}
          minRows={12}
          autosize
          value={content}
          onChange={(e) => setContent(e.target.value)}
          styles={{
            input: {
              fontFamily: MONO,
              backgroundColor: '#1c1c1e',
              color: '#e8e3d9',
              border: '1px solid #3a3a3c',
              padding: '16px',
              fontSize: '14px',
              borderRadius: '12px',
            },
          }}
        />

        {parsedLines.length > 0 && (
          <Box>
            <Text size="xs" c="#aeaeb2" mb={8} style={{ fontFamily: MONO }}>
              {validCount} cards{malformedCount > 0 && ` · ${malformedCount} malformed`}
            </Text>
            <Stack gap={2}>
              {parsedLines.map((line, i) => (
                <Box key={i} px="xs" py={4}>
                  {line.malformed ? (
                    <Group gap="xs" wrap="nowrap">
                      <Text size="xs" c="#aeaeb2" style={{ fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {line.raw}
                      </Text>
                      <Text size="xs" c="#636366" style={{ flexShrink: 0 }}>— missing ::</Text>
                    </Group>
                  ) : (
                    <Group gap="md" wrap="nowrap">
                      <Text size="xs" fw={700} c="#e8e3d9" style={{ minWidth: 80, maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: MONO }}>
                        {line.word}
                      </Text>
                      {line.note && (
                        <Text size="xs" c="#aeaeb2" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                          {line.note}
                        </Text>
                      )}
                      {line.sentence && (
                        <Text size="xs" c="#aeaeb2" fs="italic" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                          "{line.sentence}"
                        </Text>
                      )}
                    </Group>
                  )}
                </Box>
              ))}
            </Stack>
          </Box>
        )}

        <Button
          fullWidth size="xl" radius="md"
          onClick={handleBatchSubmit}
          loading={loading}
          disabled={!parsedLines.length}
          style={{
            background: parsedLines.length ? '#c79968' : '#2c2c2e',
            border: 'none',
            height: 56,
            color: parsedLines.length ? '#1c1c1e' : '#636366',
            fontWeight: 700,
          }}
        >
          Import {validCount > 0 ? `${validCount} Cards` : ''}
        </Button>
      </Stack>
    </PageShell>
  );
}
