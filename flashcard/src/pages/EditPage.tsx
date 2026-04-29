import { useState } from 'react';
import {
  Container, TextInput, Stack, Paper, Text, Textarea,
  Button, ActionIcon, Group, Title, ThemeIcon, Box, ScrollArea
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconArrowLeft, IconSearch, IconEdit, IconCheck } from '@tabler/icons-react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import type { Card } from '../types';

export default function EditPage() {
  const navigate = useNavigate();

  const [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState<Card[]>([]);
  const [selected, setSelected] = useState<Card | null>(null);
  const [sentence, setSentence] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);

  const handleSearch = async (q: string) => {
    setQuery(q);
    setSelected(null);
    if (!q.trim()) { setCandidates([]); return; }
    try {
      const results = await api.searchCards(q);
      setCandidates(results);
    } catch {
      notifications.show({ title: 'Search failed', message: 'Check network connection', color: 'red' });
    }
  };

  const handleSelect = (card: Card) => {
    setSelected(card);
    setSentence(card.sentence);
    setNote(card.note);
    setQuery(card.word);
    setCandidates([]);
  };

  const handleUpdate = async () => {
    if (!selected) return;
    setSaving(true);
    try {
      await api.updateCard(selected.id, { sentence, note });
      setSelected({ ...selected, sentence, note });
      notifications.show({ message: `${selected.word} updated`, color: 'green', icon: <IconCheck size={14} /> });
    } catch {
      notifications.show({ title: 'Update failed', message: 'Check network connection', color: 'red' });
    } finally {
      setSaving(false);
    }
  };

  const isDirty = selected && (sentence !== selected.sentence || note !== selected.note);

  return (
    <Container size="xs" py="xl" px="md">
      <Stack gap="lg">
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
            <Title order={2} c="#e8eaf0" style={{ letterSpacing: '-0.5px' }}>Edit</Title>
          </Group>
          <ThemeIcon variant="light" color="blue" size="lg" radius="md">
            <IconEdit size={20} />
          </ThemeIcon>
        </Group>

        {/* Search */}
        <Box style={{ position: 'relative' }}>
          <TextInput
            placeholder="Search a word..."
            leftSection={<IconSearch size={16} />}
            value={query}
            onChange={e => handleSearch(e.target.value)}
            styles={{
              input: {
                backgroundColor: 'rgba(255,255,255,0.04)',
                border: '1px solid rgba(255,255,255,0.1)',
                color: '#e8eaf0',
              }
            }}
          />

          {candidates.length > 0 && (
            <Paper
              style={{
                position: 'absolute',
                top: '100%',
                left: 0,
                right: 0,
                zIndex: 100,
                background: '#161b2c',
                border: '1px solid rgba(255,255,255,0.1)',
                borderTop: 'none',
                borderRadius: '0 0 8px 8px',
                overflow: 'hidden',
              }}
            >
              <ScrollArea mah={240}>
                {candidates.map(card => (
                  <Box
                    key={card.id}
                    px="md"
                    py="md"
                    onClick={() => handleSelect(card)}
                    style={{ cursor: 'pointer', borderBottom: '1px solid rgba(255,255,255,0.05)' }}
                    onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.05)')}
                    onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                  >
                    <Text size="sm" c="#e8eaf0" fw={600} style={{ fontFamily: 'JetBrains Mono' }}>
                      {card.word}
                    </Text>
                    {card.note && (
                      <Text size="xs" c="dimmed" truncate>{card.note}</Text>
                    )}
                  </Box>
                ))}
              </ScrollArea>
            </Paper>
          )}
        </Box>

        {/* Edit form */}
        {selected && (
          <Paper
            p="lg"
            radius={16}
            style={{
              background: 'linear-gradient(145deg, #161b2c 0%, #0d111d 100%)',
              border: '1px solid rgba(74,143,255,0.3)',
            }}
          >
            <Stack gap="md">
              <Text fw={700} size="xl" c="#e8eaf0" style={{ fontFamily: 'JetBrains Mono' }}>
                {selected.word}
              </Text>

              <Box>
                <Text size="xs" c="dimmed" mb={4}>Sentence</Text>
                <Textarea
                  autosize
                  minRows={2}
                  value={sentence}
                  onChange={e => setSentence(e.target.value)}
                  styles={{
                    input: {
                      backgroundColor: 'rgba(0,0,0,0.2)',
                      border: '1px solid rgba(255,255,255,0.06)',
                      color: '#e8eaf0',
                      fontSize: 14,
                    }
                  }}
                />
              </Box>

              <Box>
                <Text size="xs" c="dimmed" mb={4}>Note</Text>
                <Textarea
                  autosize
                  minRows={2}
                  value={note}
                  onChange={e => setNote(e.target.value)}
                  styles={{
                    input: {
                      backgroundColor: 'rgba(0,0,0,0.2)',
                      border: '1px solid rgba(255,255,255,0.06)',
                      color: '#e8eaf0',
                      fontSize: 14,
                    }
                  }}
                />
              </Box>

              <Button
                size="lg"
                radius="md"
                fullWidth
                disabled={!isDirty}
                loading={saving}
                onClick={handleUpdate}
                style={{
                  background: isDirty ? 'linear-gradient(135deg, #1a4fc7, #2d7aff)' : undefined,
                }}
              >
                Update
              </Button>
            </Stack>
          </Paper>
        )}
      </Stack>
    </Container>
  );
}
