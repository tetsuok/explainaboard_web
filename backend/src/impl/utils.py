from flask import abort, jsonify


def abort_with_error_message(status_code: int, err_message: str, err_code=-1):
    """
    abort with a status code, an error code and a message
    Parameters:
      - status_code: HTTP status code
      - detail: error message
      - err_code: an error code to give more detailed information. Default is -1. An example
      usage is: return xxxxx as err_code when a username is taken. This allows the client to
      gracefully handle the exception.
    """
    response = jsonify({"error_code": err_code, "detail": err_message})
    response.status_code = status_code
    abort(response)