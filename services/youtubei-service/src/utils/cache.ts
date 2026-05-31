export class TTLCache<T> {
  private readonly map = new Map<string, { value: T; expiresAt: number }>();
  private hits = 0;
  private misses = 0;

  constructor(private readonly ttlMs: number) {}

  get(key: string): T | null {
    const found = this.map.get(key);
    if (!found) {
      this.misses += 1;
      return null;
    }

    if (found.expiresAt <= Date.now()) {
      this.map.delete(key);
      this.misses += 1;
      return null;
    }

    this.hits += 1;
    return found.value;
  }

  set(key: string, value: T): void {
    this.map.set(key, { value, expiresAt: Date.now() + this.ttlMs });
  }

  stats() {
    return {
      size: this.map.size,
      hits: this.hits,
      misses: this.misses
    };
  }
}
