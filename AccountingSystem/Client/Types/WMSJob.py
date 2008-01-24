# $Header: /tmp/libdirac/tmp.stZoy15380/dirac/DIRAC3/DIRAC/AccountingSystem/Client/Types/Attic/WMSJob.py,v 1.4 2008/01/24 18:50:01 acasajus Exp $
__RCSID__ = "$Id: WMSJob.py,v 1.4 2008/01/24 18:50:01 acasajus Exp $"

from DIRAC.AccountingSystem.Client.Types.BaseAccountingType import BaseAccountingType

class WMSJob( BaseAccountingType ):

  def __init__( self ):
    BaseAccountingType.__init__( self )
    self.definitionKeyFields = [ ( 'k1', "VARCHAR(265)" ),
                                 ( 'k2', "VARCHAR(10)" )
                               ]
    self.definitionAccountingFields = [ ( 'v1', "FLOAT" ),
                                        ( 'v2', "DOUBLE" ),
                                        ( 'v3', "TINYINT" )
                                      ]
    self.checkType()