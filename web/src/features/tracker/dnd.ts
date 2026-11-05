import { closestCenter, closestCorners, getFirstCollision, pointerWithin, rectIntersection, type CollisionDetection } from '@dnd-kit/core';
import type { SortingStrategy } from '@dnd-kit/sortable';

/** While a list is in due-date order its cards hold still: the date decides the position, not the drag. */
export const holdStill: SortingStrategy = () => null;

/**
 * Collision for lists of cards in several containers (board columns, backlog sprints).
 * The container under the pointer wins; inside it, the nearest card. Over a container's empty space the
 * container itself is the target, so a short column still accepts a drop anywhere along its height.
 * Keyboard drags have no pointer and fall back to rectangle intersection, then corners.
 */
export function containerCollision(cols: Record<string, string[]>): CollisionDetection {
  return (args) => {
    const byPointer = pointerWithin(args);
    const hits = byPointer.length ? byPointer : rectIntersection(args);
    let overId = getFirstCollision(hits.length ? hits : closestCorners(args), 'id');
    if (overId == null) return [];
    const container = String(overId) in cols ? String(overId) : null;
    if (container) {
      const items = cols[container];
      if (items.length) {
        const inside = args.droppableContainers.filter((c) => items.includes(String(c.id)));
        overId = closestCenter({ ...args, droppableContainers: inside })[0]?.id ?? overId;
      }
    }
    return [{ id: overId }];
  };
}
