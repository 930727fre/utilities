import { useEffect, useState, useRef } from 'react';
import type { ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Stack, Title, Text, Button, Group, Box, FileButton,
} from '@mantine/core';
import { useNavigate } from 'react-router-dom';
import { IconUpload } from '@tabler/icons-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../api';
import type { ErrorCandidate, GraduateCandidate, PracticeStep, UploadMode } from '../types';
import PageShell from '../components/PageShell';
import CardShell from '../components/CardShell';

// Local step adds 'upload' (transient — not persisted server-side). The backend
// step machine is roleplay → additions → graduations; 'upload' lives only on the
// frontend between "Done practicing" and the per-card review screens.
type LocalStep = 'init' | PracticeStep | 'upload';

export default function PracticePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<LocalStep>('init');
  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [mode, setMode] = useState<UploadMode>('roleplay');
  const [sessionId, setSessionId] = useState<string | null>(null);

  // On mount, ask the server where to land. Drives resume-after-bail: if there's
  // a pending session, we jump straight to the additions/graduations swipe.
  const { data: practiceState } = useQuery({
    queryKey: ['practice-state'],
    queryFn: api.getPracticeState,
    staleTime: 0,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });

  useEffect(() => {
    if (step === 'init' && practiceState) {
      setStep(practiceState.step);
      if (practiceState.session_id) setSessionId(practiceState.session_id);
    }
  }, [practiceState, step]);

  const finish = () => {
    queryClient.invalidateQueries({ queryKey: ['today-stats'] });
    queryClient.invalidateQueries({ queryKey: ['today-roleplay'] });
    queryClient.invalidateQueries({ queryKey: ['practice-state'] });
    navigate('/');
  };

  return (
    <PageShell scroll="locked">
      <Stack gap="md" style={{ flex: 1, minHeight: 0 }}>
        {step === 'init' && (
          <CardShell><Box p="lg"><Text c="var(--text-dim)">Loading...</Text></Box></CardShell>
        )}
        {step === 'roleplay' && (
          <RoleplayStep onDone={(m) => { setMode(m); setStep('upload'); }} />
        )}
        {step === 'upload' && (
          <UploadStep
            file={audioFile}
            mode={mode}
            onFileChange={setAudioFile}
            onUploaded={(sid) => {
              setSessionId(sid);
              setStep('additions');
            }}
          />
        )}
        {step === 'additions' && sessionId && (
          <AdditionsStep
            sessionId={sessionId}
            onComplete={() => setStep('graduations')}
          />
        )}
        {step === 'graduations' && sessionId && (
          <GraduationsStep
            sessionId={sessionId}
            onComplete={finish}
          />
        )}
      </Stack>
    </PageShell>
  );
}


// ─── Step 1: Roleplay ────────────────────────────────────────────────────────

function RoleplayStep({ onDone }: { onDone: (mode: UploadMode) => void }) {
  const { data: roleplay, isLoading, isFetching } = useQuery({
    queryKey: ['today-roleplay'],
    queryFn: api.getTodayRoleplay,
    // Without this, every Practice mount refetches and the cached-data
    // stale-while-revalidate window briefly flashes the "regenerating" pulse
    // even when nothing's changed. 5 min stays cached, then naturally refreshes
    // when the user comes back after a real break. Page reload (F5) clears the
    // in-memory cache and triggers a fresh fetch regardless.
    staleTime: 5 * 60 * 1000,
  });
  // Cached data is shown while a background refetch is in flight (stale-while-revalidate).
  // Surface that to the user with a pulsing indicator so they know an Opus regen is happening.
  const refreshing = isFetching && !isLoading;

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
          {refreshing && (
            <Text c="var(--text)" className="glyph-pulse"
              style={{ fontFamily: 'var(--mono)', fontSize: 13 }}>
              ○ regenerating roleplay…
            </Text>
          )}
          {roleplay && (
            <Box className="roleplay-md" c="var(--text-h)"
              style={{ fontSize: 16, lineHeight: 1.6 }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {roleplay.script}
              </ReactMarkdown>
            </Box>
          )}
        </Stack>
      </CardShell>
      <Group grow gap="sm">
        <Button
          size="lg"
          radius={8}
          onClick={() => onDone('freestyle')}
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
          onClick={() => onDone('roleplay')}
          disabled={!roleplay || refreshing}
          style={{
            background: 'var(--accent)',
            color: 'var(--bg)',
            height: 54,
            fontFamily: 'var(--mono)',
            opacity: !roleplay || refreshing ? 0.5 : 1,
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
  file, mode, onFileChange, onUploaded,
}: {
  file: File | null;
  mode: UploadMode;
  onFileChange: (f: File | null) => void;
  onUploaded: (sessionId: string) => void;
}) {
  const resetRef = useRef<() => void>(() => {});
  const queryClient = useQueryClient();

  const upload = useMutation({
    mutationFn: () => {
      if (!file) throw new Error('No file selected');
      return api.uploadAudio(file, mode);
    },
    onSuccess: async (result) => {
      // Freshly-uploaded session means /today/review now returns its candidates.
      // Invalidate so the next mount of AdditionsStep refetches.
      await queryClient.invalidateQueries({ queryKey: ['today-review'] });
      onUploaded(result.session_id);
    },
  });

  return (
    <>
      <CardShell>
        <Stack gap="lg" align="center" justify="center" p={{ base: 'lg', sm: 32 }}
          style={{ flex: 1, minHeight: 0 }}>
          <Text c="var(--text-dim)" size="xs"
            style={{ fontFamily: 'var(--mono)', letterSpacing: 2, textTransform: 'uppercase' }}>
            upload recording · mode: {mode}
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
  sessionId, onComplete,
}: {
  sessionId: string;
  onComplete: () => void;
}) {
  const queryClient = useQueryClient();
  const { data: review, isLoading, isFetching } = useQuery({
    queryKey: ['today-review'],
    queryFn: api.getReview,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  // Snapshot the candidates returned at mount and iterate locally — the
  // server-side review query filters by decisions, so a refetch would
  // shrink the list out from under us mid-swipe. Wait for `!isFetching`
  // so we don't capture stale-cached data from a just-invalidated query.
  const [snapshot, setSnapshot] = useState<ErrorCandidate[] | null>(null);
  useEffect(() => {
    if (snapshot === null && review && !isFetching) setSnapshot(review.additions);
  }, [review, isFetching, snapshot]);

  const [index, setIndex] = useState(0);

  const decide = useMutation({
    mutationFn: (args: { candidateId: string; action: 'added' | 'skipped' }) =>
      api.decide(sessionId, args.candidateId, args.action),
    onError: (err: Error) => {
      // Surface in console for now; user will see the disabled state lift on next render.
      console.error('[additions] decide failed:', err.message);
    },
  });

  if (isLoading || snapshot === null) {
    return <CardShell><Box p="lg"><Text c="var(--text-dim)">Loading...</Text></Box></CardShell>;
  }

  if (snapshot.length === 0) {
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
        <Button size="lg" radius={8}
          onClick={() => {
            queryClient.invalidateQueries({ queryKey: ['today-review'] });
            onComplete();
          }}
          style={{ background: 'var(--accent)', color: 'var(--bg)', height: 54, fontFamily: 'var(--mono)' }}>
          Continue to graduate
        </Button>
      </>
    );
  }

  const current = snapshot[index];

  async function recordAndAdvance(action: 'added' | 'skipped') {
    await decide.mutateAsync({ candidateId: current.id, action });
    if (index < snapshot!.length - 1) {
      setIndex(index + 1);
    } else {
      queryClient.invalidateQueries({ queryKey: ['today-review'] });
      onComplete();
    }
  }

  return <CandidateStack
    label="new errors"
    position={`${index + 1} / ${snapshot.length}`}
    title={current.title}
    body={<AdditionBody c={current} />}
    leftLabel="Skip"
    rightLabel="Add"
    onLeft={() => recordAndAdvance('skipped')}
    onRight={() => recordAndAdvance('added')}
    pending={decide.isPending}
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
  sessionId, onComplete,
}: {
  sessionId: string;
  onComplete: () => void;
}) {
  const queryClient = useQueryClient();
  const { data: review, isLoading, isFetching } = useQuery({
    queryKey: ['today-review'],
    queryFn: api.getReview,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const [snapshot, setSnapshot] = useState<GraduateCandidate[] | null>(null);
  useEffect(() => {
    if (snapshot === null && review && !isFetching) setSnapshot(review.graduations);
  }, [review, isFetching, snapshot]);

  const [index, setIndex] = useState(0);

  const decide = useMutation({
    mutationFn: (args: { candidateId: string; action: 'graduated' | 'kept' }) =>
      api.decide(sessionId, args.candidateId, args.action),
    onError: (err: Error) => {
      console.error('[graduations] decide failed:', err.message);
    },
  });

  if (isLoading || snapshot === null) {
    return <CardShell><Box p="lg"><Text c="var(--text-dim)">Loading...</Text></Box></CardShell>;
  }

  if (snapshot.length === 0) {
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
        <Button size="lg" radius={8}
          onClick={() => {
            queryClient.invalidateQueries({ queryKey: ['today-review'] });
            onComplete();
          }}
          style={{ background: 'var(--accent)', color: 'var(--bg)', height: 54, fontFamily: 'var(--mono)' }}>
          Finish
        </Button>
      </>
    );
  }

  const current = snapshot[index];

  async function recordAndAdvance(action: 'graduated' | 'kept') {
    await decide.mutateAsync({ candidateId: current.id, action });
    if (index < snapshot!.length - 1) {
      setIndex(index + 1);
    } else {
      queryClient.invalidateQueries({ queryKey: ['today-review'] });
      onComplete();
    }
  }

  return <CandidateStack
    label="graduate?"
    position={`${index + 1} / ${snapshot.length}`}
    title={current.title}
    body={<GraduationBody c={current} />}
    leftLabel="Keep"
    rightLabel="Graduate"
    onLeft={() => recordAndAdvance('kept')}
    onRight={() => recordAndAdvance('graduated')}
    pending={decide.isPending}
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
