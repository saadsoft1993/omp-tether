// Stand-in for the stats package's live-data stream: this app has no event stream, so the data version never moves.
export function currentLiveVersion(): number {
	return 0;
}

export function useLiveVersion(_enabled = true): number {
	return 0;
}
