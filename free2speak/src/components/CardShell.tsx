import type { ReactNode, CSSProperties, MouseEventHandler } from 'react';
import { Paper } from '@mantine/core';

interface CardShellProps {
  children: ReactNode;
  onClick?: MouseEventHandler<HTMLDivElement>;
  style?: CSSProperties;
}

export default function CardShell({ children, onClick, style }: CardShellProps) {
  return (
    <Paper
      p={0}
      radius={24}
      onClick={onClick}
      style={{
        flex: 1,
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--card)',
        border: '1px solid var(--border)',
        boxShadow: 'var(--shadow)',
        overflowY: 'auto',
        cursor: onClick ? 'pointer' : 'default',
        ...style,
      }}
    >
      {children}
    </Paper>
  );
}
