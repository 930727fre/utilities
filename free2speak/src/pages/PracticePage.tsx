import { useState, useRef } from 'react';
import type { ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
// upload's onSuccess refetches /today/review so the additions/graduations screens
// see the analysis we just persisted (not the empty fetch from page mount).
import {
  Stack, Title, Text, Button, Group, Box, FileButton,
} from '@mantine/core';
import { useNavigate } from 'react-router-dom';
import { IconUpload } from '@tabler/icons-react';
import { api } from '../api';
import type { ErrorCandidate, GraduateCandidate } from '../types';
import PageShell from '../components/PageShell';
import CardShell from '../components/CardShell';

type Step = 'roleplay' | 'upload' | 'additions' | 'graduations';

export default function PracticePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>('roleplay');
  const [audioFile, setAudioFile] = useState<File | null>(null);

  const { data: review } = useQuery({
    queryKey: ['today-review'],
    queryFn: api.getReview,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });

  const finish = () => {
    queryClient.invalidateQueries({ queryKey: ['today-stats'] });
    navigate('/');
  };

  return (
    <PageShell scroll="locked">
      <Stack gap="md" style={{ flex: 1, minHeight: 0 }}>
        {step === 'roleplay' && (
          <RoleplayStep onDone={() => setStep('upload')} />
        )}
        {step === 'upload' && (
          <UploadStep
            file={audioFile}
            onFileChange={setAudioFile}
            onUploaded={() => setStep('additions')}
          />
        )}
        {step === 'additions' && (
          <AdditionsStep
            candidates={review?.additions}
            onComplete={() => setStep('graduations')}
          />
        )}
        {step === 'graduations' && (
          <GraduationsStep
            candidates={review?.graduations}
            onComplete={finish}
          />
        )}
      </Stack>
    </PageShell>
  );
}


// ─── Step 1: Roleplay ────────────────────────────────────────────────────────

function RoleplayStep({ onDone }: { onDone: () => void }) {
  const { data: roleplay, isLoading } = useQuery({
    queryKey: ['today-roleplay'],
    queryFn: api.getTodayRoleplay,
  });

  return (
    <>
      <CardShell>
        <Stack gap="md" p={{ base: 'lg', sm: 32 }} style={{ flex: 1, minHeight: 0 }}>
          <Box>
            <Text c="var(--text-dim)" size="xs"
              style={{ fontFamily: 'var(--mono)', letterSpacing: 2, textTransform: 'uppercase' }}>
              {roleplay ? `${roleplay.date} · ${roleplay.topic}` : 'loading...'}
            </Text>
            {roleplay && (
              <Text c="var(--text)" size="sm" mt="xs" style={{ fontFamily: 'var(--mono)' }}>
                {roleplay.rationale}
              </Text>
            )}
          </Box>
          {isLoading && <Text c="var(--text-dim)">Loading...</Text>}
          {roleplay && (
            <Text c="var(--text-h)" style={{ whiteSpace: 'pre-wrap', fontSize: 16, lineHeight: 1.6 }}>
              {roleplay.script}
            </Text>
          )}
        </Stack>
      </CardShell>
      <Group grow gap="sm">
        <Button
          size="lg"
          radius={8}
          onClick={onDone}
          style={{
            background: 'transparent',
            color: 'var(--text-h)',
            border: '1px solid var(--border)',
            height: 54,
            fontFamily: 'var(--mono)',
          }}
        >
          Skip (free chat)
        </Button>
        <Button
          size="lg"
          radius={8}
          onClick={onDone}
          disabled={!roleplay}
          style={{
            background: 'var(--accent)',
            color: 'var(--bg)',
            height: 54,
            fontFamily: 'var(--mono)',
          }}
        >
          Done practicing
        </Button>
      </Group>
    </>
  );
}


// ─── Step 2: Upload ──────────────────────────────────────────────────────────

function UploadStep({
  file, onFileChange, onUploaded,
}: {
  file: File | null;
  onFileChange: (f: File | null) => void;
  onUploaded: () => void;
}) {
  const resetRef = useRef<() => void>(() => {});
  const queryClient = useQueryClient();

  const upload = useMutation({
    mutationFn: () => {
      if (!file) throw new Error('No file selected');
      return api.uploadAudio(file);
    },
    onSuccess: async () => {
      // Pull a fresh /today/review now that a session exists — the page-mount
      // fetch happened before upload, so its result was empty.
      await queryClient.invalidateQueries({ queryKey: ['today-review'] });
      onUploaded();
    },
  });

  return (
    <>
      <CardShell>
        <Stack gap="lg" align="center" justify="center" p={{ base: 'lg', sm: 32 }}
          style={{ flex: 1, minHeight: 0 }}>
          <Text c="var(--text-dim)" size="xs"
            style={{ fontFamily: 'var(--mono)', letterSpacing: 2, textTransform: 'uppercase' }}>
            upload recording
          </Text>

          <FileButton
            resetRef={resetRef}
            onChange={onFileChange}
            accept="audio/*,.m4a,.mp3,.wav,.aac"
          >
            {(props) => (
              <Button
                {...props}
                variant="outline"
                size="lg"
                radius={8}
                leftSection={<IconUpload size={18} />}
                style={{
                  borderColor: 'var(--border)',
                  color: 'var(--text-h)',
                  background: 'transparent',
                  fontFamily: 'var(--mono)',
                }}
              >
                {file ? 'Choose different file' : 'Choose audio file'}
              </Button>
            )}
          </FileButton>

          {file && (
            <Text c="var(--text-h)" size="sm" style={{ fontFamily: 'var(--mono)' }}>
              ✓ {file.name} ({(file.size / 1024 / 1024).toFixed(2)} MB)
            </Text>
          )}

          {upload.isPending && (
            <Text c="var(--text)" className="glyph-pulse"
              style={{ fontFamily: 'var(--mono)' }}>
              ○ Analyzing with Gemini ...
            </Text>
          )}
          {upload.isError && (
            <Text c="#ff6b6b" size="sm">
              ! Upload failed: {(upload.error as Error).message}
            </Text>
          )}
        </Stack>
      </CardShell>
      <Button
        size="lg"
        radius={8}
        disabled={!file || upload.isPending}
        onClick={() => upload.mutate()}
        style={{
          background: file ? 'var(--accent)' : 'var(--card)',
          color: file ? 'var(--bg)' : 'var(--text-dim)',
          height: 54,
          fontFamily: 'var(--mono)',
        }}
      >
        Analyze
      </Button>
    </>
  );
}


// ─── Step 3: Additions ───────────────────────────────────────────────────────

function AdditionsStep({
  candidates, onComplete,
}: {
  candidates: ErrorCandidate[] | undefined;
  onComplete: () => void;
}) {
  const [index, setIndex] = useState(0);
  const [addedIds, setAddedIds] = useState<string[]>([]);

  const apply = useMutation({
    mutationFn: (ids: string[]) => api.applyAdditions(ids),
    onSuccess: () => onComplete(),
  });

  if (!candidates) {
    return <CardShell><Box p="lg"><Text c="var(--text-dim)">Loading...</Text></Box></CardShell>;
  }

  if (candidates.length === 0) {
    return (
      <>
        <CardShell>
          <Stack align="center" justify="center" p={{ base: 'lg', sm: 40 }} style={{ flex: 1 }}>
            <Text c="var(--text-dim)" size="xs"
              style={{ fontFamily: 'var(--mono)', letterSpacing: 2, textTransform: 'uppercase' }}>
              no new errors
            </Text>
            <Title order={2} c="var(--text-h)" ff="var(--mono)">
              ✓ Nothing to add
            </Title>
          </Stack>
        </CardShell>
        <Button size="lg" radius={8} onClick={onComplete}
          style={{ background: 'var(--accent)', color: 'var(--bg)', height: 54, fontFamily: 'var(--mono)' }}>
          Continue to graduate
        </Button>
      </>
    );
  }

  const current = candidates[index];
  const isLast = index === candidates.length - 1;

  const handleSkip = () => {
    nextOrCommit(false);
  };
  const handleAdd = () => {
    setAddedIds((p) => [...p, current.id]);
    nextOrCommit(true);
  };

  function nextOrCommit(justAdded: boolean) {
    if (isLast) {
      const finalAdds = justAdded ? [...addedIds, current.id] : addedIds;
      apply.mutate(finalAdds);
    } else {
      setIndex((i) => i + 1);
    }
  }

  return <CandidateStack
    label="new errors"
    position={`${index + 1} / ${candidates.length}`}
    title={current.title}
    body={<AdditionBody c={current} />}
    leftLabel="Skip"
    rightLabel="Add"
    onLeft={handleSkip}
    onRight={handleAdd}
    pending={apply.isPending}
  />;
}

// Split a sentence on `"..."` segments and wrap the quoted portions in a
// styled span. Gemini wraps the specific error (in you_said) or correction
// (in native) with double quotes so the diff is visible at a glance.
function highlightQuoted(text: string, highlightColor: string): ReactNode {
  const parts: ReactNode[] = [];
  const re = /"([^"]+)"/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    parts.push(
      <span key={key++} style={{ color: highlightColor, fontWeight: 600 }}>
        {match[1]}
      </span>
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts.length > 0 ? parts : text;
}

// When a card groups multiple instances of one pattern, Gemini joins them with
// ' / '. Split-render each on its own line so they're scannable.
function MultiLineHighlight({ text, color, italic }: { text: string; color: string; italic?: boolean }) {
  const lines = text.split(' / ');
  return (
    <Stack gap={6} mt="xs">
      {lines.map((line, i) => (
        <Text key={i} c="var(--text-h)" style={{ fontStyle: italic ? 'italic' : undefined }}>
          {highlightQuoted(line, color)}
        </Text>
      ))}
    </Stack>
  );
}

function AdditionBody({ c }: { c: ErrorCandidate }) {
  return (
    <Stack gap="md" mt="lg">
      <Box>
        <Text c="var(--text-dim)" size="xs"
          style={{ fontFamily: 'var(--mono)', letterSpacing: 1, textTransform: 'uppercase' }}>
          you said
        </Text>
        <MultiLineHighlight text={c.you_said} color="#ff8a8a" italic />
      </Box>
      <Box>
        <Text c="var(--text-dim)" size="xs"
          style={{ fontFamily: 'var(--mono)', letterSpacing: 1, textTransform: 'uppercase' }}>
          native
        </Text>
        <MultiLineHighlight text={c.native} color="var(--accent)" />
      </Box>
    </Stack>
  );
}


// ─── Step 4: Graduations ─────────────────────────────────────────────────────

function GraduationsStep({
  candidates, onComplete,
}: {
  candidates: GraduateCandidate[] | undefined;
  onComplete: () => void;
}) {
  const [index, setIndex] = useState(0);
  const [gradIds, setGradIds] = useState<string[]>([]);

  const apply = useMutation({
    mutationFn: (ids: string[]) => api.applyGraduations(ids),
    onSuccess: () => onComplete(),
  });

  if (!candidates) {
    return <CardShell><Box p="lg"><Text c="var(--text-dim)">Loading...</Text></Box></CardShell>;
  }

  if (candidates.length === 0) {
    return (
      <>
        <CardShell>
          <Stack align="center" justify="center" p={{ base: 'lg', sm: 40 }} style={{ flex: 1 }}>
            <Text c="var(--text-dim)" size="xs"
              style={{ fontFamily: 'var(--mono)', letterSpacing: 2, textTransform: 'uppercase' }}>
              no graduations
            </Text>
            <Title order={2} c="var(--text-h)" ff="var(--mono)">
              ✓ Nothing to graduate
            </Title>
          </Stack>
        </CardShell>
        <Button size="lg" radius={8} onClick={onComplete}
          style={{ background: 'var(--accent)', color: 'var(--bg)', height: 54, fontFamily: 'var(--mono)' }}>
          Finish
        </Button>
      </>
    );
  }

  const current = candidates[index];
  const isLast = index === candidates.length - 1;

  const handleKeep = () => {
    nextOrCommit(false);
  };
  const handleGrad = () => {
    setGradIds((p) => [...p, current.id]);
    nextOrCommit(true);
  };

  function nextOrCommit(justGrad: boolean) {
    if (isLast) {
      const finalGrads = justGrad ? [...gradIds, current.id] : gradIds;
      apply.mutate(finalGrads);
    } else {
      setIndex((i) => i + 1);
    }
  }

  return <CandidateStack
    label="graduate?"
    position={`${index + 1} / ${candidates.length}`}
    title={current.title}
    body={<GraduationBody c={current} />}
    leftLabel="Keep"
    rightLabel="Graduate"
    onLeft={handleKeep}
    onRight={handleGrad}
    pending={apply.isPending}
  />;
}

function GraduationBody({ c }: { c: GraduateCandidate }) {
  return (
    <Stack gap="md" mt="lg">
      <Box>
        <Text c="var(--text-dim)" size="xs"
          style={{ fontFamily: 'var(--mono)', letterSpacing: 1, textTransform: 'uppercase' }}>
          evidence
        </Text>
        <Text c="var(--text-h)" mt="xs" style={{ fontStyle: 'italic' }}>
          "{c.evidence}"
        </Text>
      </Box>
      {c.occurrences !== undefined && (
        <Text c="var(--text-dim)" size="sm" style={{ fontFamily: 'var(--mono)' }}>
          ({c.occurrences} correct {c.occurrences === 1 ? 'use' : 'uses'})
        </Text>
      )}
    </Stack>
  );
}


// ─── Shared: card with two action buttons ────────────────────────────────────

interface CandidateStackProps {
  label: string;
  position: string;
  title: string;
  body: ReactNode;
  leftLabel: string;
  rightLabel: string;
  onLeft: () => void;
  onRight: () => void;
  pending: boolean;
}

function CandidateStack({
  label, position, title, body, leftLabel, rightLabel, onLeft, onRight, pending,
}: CandidateStackProps) {
  return (
    <>
      <CardShell>
        <Stack gap="md" p={{ base: 'lg', sm: 32 }} style={{ flex: 1, minHeight: 0 }}>
          <Group justify="space-between">
            <Text c="var(--text-dim)" size="xs"
              style={{ fontFamily: 'var(--mono)', letterSpacing: 2, textTransform: 'uppercase' }}>
              {label}
            </Text>
            <Text c="var(--text-dim)" size="xs" style={{ fontFamily: 'var(--mono)' }}>
              {position}
            </Text>
          </Group>
          <Title order={2} c="var(--text-h)"
            style={{ fontFamily: 'var(--mono)', fontSize: 22, lineHeight: 1.3 }}>
            {title}
          </Title>
          {body}
        </Stack>
      </CardShell>
      <Group grow gap="sm">
        <Button
          size="lg"
          radius={8}
          disabled={pending}
          onClick={onLeft}
          style={{
            background: 'transparent',
            color: 'var(--text-h)',
            border: '1px solid var(--border)',
            height: 54,
            fontFamily: 'var(--mono)',
          }}
        >
          {leftLabel}
        </Button>
        <Button
          size="lg"
          radius={8}
          disabled={pending}
          onClick={onRight}
          style={{
            background: 'var(--accent)',
            color: 'var(--bg)',
            height: 54,
            fontFamily: 'var(--mono)',
          }}
        >
          {rightLabel}
        </Button>
      </Group>
    </>
  );
}
