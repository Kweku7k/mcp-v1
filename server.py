from typing import Any
from fastmcp import FastMCP
from presto_client import PrestoQAPIError, PrestoQClient


mcp = FastMCP()
presto_client = PrestoQClient()


@mcp.tool()
async def login(email: str, password: str) -> dict[str, Any]:
    """
    Sign in to PrestoQ and persist the bearer token for future requests.
    """
    print(f"[REQUEST] login() - email: {email}")
    try:
        result = await presto_client.login(email=email, password=password)
        token = result["token"]

        response = {
            "success": True,
            "message": "Authenticated successfully. Token saved for reuse.",
            "token_preview": f"{token[:8]}..." if len(token) >= 8 else "***",
        }
        print(f"[RESPONSE] login() - success")
        return response
    except PrestoQAPIError as exc:
        print(f"[ERROR] login() - {str(exc)}")
        return {
            "success": False,
            "error": str(exc),
        }


@mcp.tool()
async def get_all_orders() -> dict[str, Any]:
    """
    Retrieve orders from PrestoQ.

    Use this tool when the user asks to see, search, filter, count,
    summarise, or analyse orders.

    Args:
        page: Page number to retrieve. Starts at 1.
        per_page: Number of orders per page. Maximum 100.
        status: Optional order status, such as pending, confirmed,
            processing, ready, collected, cancelled, or completed.
        payment_status: Optional payment status, such as pending,
            paid, failed, refunded, or unpaid.
        business_id: Optional ID of the business whose orders should
            be returned.
        customer_phone: Optional customer telephone number.
        search: Optional general search term, such as an order number
            or customer name.
        sort_order: Return newest orders first with desc, or oldest
            orders first with asc.

    Returns:
        A dictionary containing the matching orders and pagination
        information.
    """
    print("[REQUEST] get_all_orders()")
    
    params: dict[str, Any] = {}
    

    try:
        api_response = await presto_client.get(
            "/orders"
        )

        orders = (
            api_response.get("orders")
            or api_response.get("data")
            or api_response.get("results")
            or []
        )
        
        response = {
            "success": True,
            "filters": {
                key: value
                for key, value in params.items()
                if value is not None
            },
            "order_count": len(orders),
            "orders": orders,
        }
        print(f"[RESPONSE] get_all_orders() - retrieved {len(orders)} orders")
        return response

    except PrestoQAPIError as exc:
        print(f"[ERROR] get_all_orders() - {str(exc)}")
        return {
            "success": False,
            "error": str(exc),
            "orders": [],
        }


if __name__ == "__main__":
    print("[SERVER] Starting MCP server on http://localhost:8010...")
    mcp.run(transport="http", port=8010)