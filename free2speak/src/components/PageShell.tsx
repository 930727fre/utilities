import type { ReactNode } from 'react';
import { Container } from '@mantine/core';
import type { MantineSize } from '@mantine/core';

type ScrollMode = 'centered' | 'top' | 'locked';

interface PageShellProps {
  children: ReactNode;
  scroll?: ScrollMode;
  size?: MantineSize;
  maw?: number | string;
}

export default function PageShell({
  children,
  scroll = 'top',
  size = 'sm',
  maw,
}: PageShellProps) {
  return (
    <Container
      size={size}
      maw={maw}
      px="md"
      w="100%"
      style={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: scroll === 'centered' ? 'center' : undefined,
        overflowY: scroll === 'top' ? 'auto' : 'hidden',
        overscrollBehavior: 'contain',
        paddingTop: 'max(16px, env(safe-area-inset-top))',
        paddingBottom: 'max(16px, env(safe-area-inset-bottom))',
      }}
    >
      {children}
    </Container>
  );
}
