import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Tray } from '@src/shared/ui/Tray';

describe('image fallback in the existing tray', () => {
  it('marks local fallback beside the original and keeps its target', () => {
    const job = { id: 'image-1', title: '截图', state: 'processing', progress: { done: 2, total: 4 },
      image_read: { local_fallback: true } };
    render(<Tray jobs={[job]} />);
    fireEvent.click(screen.getByRole('button', { name: '进度' }));
    expect(screen.getByText('本机')).toHaveAttribute('title', '本机识图');
    expect(screen.getByText('50%')).toBeInTheDocument();
  });
});
