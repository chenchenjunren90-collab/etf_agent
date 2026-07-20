from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from _publish_researched_prediction import build_prediction


def main() -> int:
    with TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        pd.DataFrame([
            {"date": "2026-07-17", "open": 3.147, "close": 3.135},
            {"date": "2026-07-20", "open": 3.140, "close": 3.242},
        ]).to_csv(data_dir / "510880.csv", index=False)
        result, submit, news = build_prediction(
            date_str="2026-07-21",
            symbol="510880",
            allocation=0.20,
            reason="公开行情显示防御方向相对强势。",
            sources=[{"title": "可靠行情", "url": "https://example.com/quote"}],
            data_dir=data_dir,
        )
        assert submit == [{"symbol": "510880", "symbol_name": "红利ETF", "volume": 30800}]
        held = result["summary"]["held_stocks"][0]
        assert held["latest_price"] == 3.242
        assert held["amount"] == 99853.6
        assert held["weight"] == 20.0
        assert result["summary"]["utilization_rate"] == 20.0
        assert result["manual_research"]["price_date"] == "2026-07-20"
        assert news["source"] == "human_public_research"

        try:
            build_prediction(
                date_str="2026-07-21",
                symbol="300757",
                allocation=0.20,
                reason="个股不允许。",
                sources=[{"title": "公告", "url": "https://example.com"}],
                data_dir=data_dir,
            )
        except ValueError as exc:
            assert "ETF 白名单" in str(exc)
        else:
            raise AssertionError("非 ETF 个股必须被拒绝")
    print("RESEARCHED PREDICTION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
