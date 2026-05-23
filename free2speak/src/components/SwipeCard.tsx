import type { ReactNode } from 'react';
import { motion, useMotionValue, useTransform } from 'framer-motion';

interface SwipeCardProps {
  children: ReactNode;
  onLeft: () => void;
  onRight: () => void;
  threshold?: number;
  disabled?: boolean;
  /** Color shown as overlay when dragging left past threshold. Default: muted gray. */
  leftHint?: string;
  /** Color shown as overlay when dragging right past threshold. Default: honey. */
  rightHint?: string;
}

/**
 * Tinder-style drag wrapper. Render the actual visual card as children.
 *
 * - Drag horizontally. Card rotates and fades as it moves.
 * - Released past `threshold` (px) → fires onLeft / onRight.
 * - Released within threshold → springs back to center.
 * - Caller is responsible for advancing the card stack on the callbacks
 *   (this component does not own the index).
 */
export default function SwipeCard({
  children,
  onLeft,
  onRight,
  threshold = 120,
  disabled = false,
  leftHint = 'rgba(170, 170, 170, 0.18)',
  rightHint = 'rgba(199, 153, 104, 0.22)',
}: SwipeCardProps) {
  const x = useMotionValue(0);
  const rotate = useTransform(x, [-300, 0, 300], [-12, 0, 12]);
  const opacity = useTransform(x, [-300, -120, 0, 120, 300], [0.3, 1, 1, 1, 0.3]);
  const leftOpacity = useTransform(x, [-threshold, 0], [1, 0]);
  const rightOpacity = useTransform(x, [0, threshold], [0, 1]);

  return (
    <motion.div
      style={{
        x,
        rotate,
        opacity,
        flex: 1,
        minHeight: 0,
        display: 'flex',
        position: 'relative',
        touchAction: 'pan-y',
      }}
      drag={disabled ? false : 'x'}
      dragConstraints={{ left: 0, right: 0 }}
      dragElastic={0.7}
      dragMomentum={false}
      onDragEnd={(_, info) => {
        if (info.offset.x > threshold) {
          onRight();
        } else if (info.offset.x < -threshold) {
          onLeft();
        }
      }}
    >
      {children}
      {/* Left hint overlay */}
      <motion.div
        style={{
          opacity: leftOpacity,
          position: 'absolute',
          inset: 0,
          background: leftHint,
          borderRadius: 24,
          pointerEvents: 'none',
        }}
      />
      {/* Right hint overlay */}
      <motion.div
        style={{
          opacity: rightOpacity,
          position: 'absolute',
          inset: 0,
          background: rightHint,
          borderRadius: 24,
          pointerEvents: 'none',
        }}
      />
    </motion.div>
  );
}
