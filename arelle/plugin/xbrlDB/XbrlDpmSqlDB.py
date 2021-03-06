'''
XbrlDpmEbaDB.py implements an SQL database interface for Arelle, based
on the DPM EBA database.  This is a semantic data points modeling 
representation of EBA's XBRL information architecture. 

This module may save directly to a Postgres, MySQL, SEQLite, MSSQL, or Oracle server.

This module provides the execution context for saving a dts and instances in 
XBRL SQL database.  It may be loaded by Arelle's RSS feed, or by individual
DTS and instances opened by interactive or command line/web service mode.

Example dialog or command line parameters for operation:

    host:  the supporting host for SQL Server
    port:  the host port of server
    user, password:  if needed for server
    database:  the top level path segment for the SQL Server
    timeout: 
    

(c) Copyright 2014 Mark V Systems Limited, California US, All rights reserved.  
Mark V copyright applies to this software, which is licensed according to the terms of Arelle(r).


to use from command line:

linux
   # be sure plugin is installed
   arelleCmdLine --plugin '+xbrlDB|show'
   arelleCmdLine -f http://sec.org/somewhere/some.rss -v --store-to-XBRL-DB 'myserver.com,portnumber,pguser,pgpasswd,database,timeoutseconds'
   
windows
   rem be sure plugin is installed
   arelleCmdLine --plugin "+xbrlDB|show"
   arelleCmdLine -f http://sec.org/somewhere/some.rss -v --store-to-XBRL-DB "myserver.com,portnumber,pguser,pgpasswd,database,timeoutseconds"

'''

import time, datetime, os
from arelle.ModelDocument import Type, create as createModelDocument
from arelle import Locale, ValidateXbrlDimensions
from arelle.ModelValue import qname, dateTime
from arelle.PrototypeInstanceObject import DimValuePrototype
from arelle.ValidateXbrlCalcs import roundValue
from arelle.XmlUtil import xmlstring, datetimeValue, addChild, addQnameValue
from arelle import XbrlConst
from .SqlDb import XPDBException, isSqlConnection, SqlDbConnection
from decimal import Decimal, InvalidOperation

def insertIntoDB(modelXbrl, 
                 user=None, password=None, host=None, port=None, database=None, timeout=None,
                 product=None, rssItem=None, loadDBsaveToFile=None, **kwargs):
    if getattr(modelXbrl, "blockDpmDBrecursion", False):
        return None
    result = None
    xbrlDbConn = None
    try:
        xbrlDbConn = XbrlSqlDatabaseConnection(modelXbrl, user, password, host, port, database, timeout, product)
        xbrlDbConn.verifyTables()
        if loadDBsaveToFile:
            # load modelDocument from database saving to file
            result = xbrlDbConn.loadXbrlFromDB(loadDBsaveToFile)
        else:
            xbrlDbConn.insertXbrl(rssItem=rssItem)
        xbrlDbConn.close()
    except Exception as ex:
        if xbrlDbConn is not None:
            try:
                xbrlDbConn.close(rollback=True)
            except Exception as ex2:
                pass
        raise # reraise original exception with original traceback 
    return result   
    
def isDBPort(host, port, timeout=10, product="postgres"):
    return isSqlConnection(host, port, timeout)

XBRLDBTABLES = {
                "dAvailableTable", "dCloseTableFact", "dOpenTableSheetsRow",
                "dProcessingContext", "dProcessingFact"
                }



class XbrlSqlDatabaseConnection(SqlDbConnection):
    def verifyTables(self):
        missingTables = XBRLDBTABLES - self.tablesInDB()
        if missingTables and missingTables != {"sequences"}:
            raise XPDBException("sqlDB:MissingTables",
                                _("The following tables are missing: %(missingTableNames)s"),
                                missingTableNames=', '.join(t for t in sorted(missingTables))) 
            
    def insertXbrl(self, rssItem):
        try:
            # must also have default dimensions loaded
            from arelle import ValidateXbrlDimensions
            ValidateXbrlDimensions.loadDimensionDefaults(self.modelXbrl)
            
            # must have a valid XBRL instance or document
            if self.modelXbrl.modelDocument is None:
                raise XPDBException("xpgDB:MissingXbrlDocument",
                                    _("No XBRL instance or schema loaded for this filing.")) 
            
            # at this point we determine what's in the database and provide new tables
            # requires locking most of the table structure
            self.lockTables(("dAvailableTable", "dCloseFactTable", "dOpenTableSheetsRow",
                             "dProcessingContext", "dProcessingFact"))
            
            self.dropTemporaryTable()
 
            startedAt = time.time()
            self.insertInstance()
            self.insertDataPoints()
            self.modelXbrl.profileStat(_("XbrlSqlDB: instance insertion"), time.time() - startedAt)
            
            startedAt = time.time()
            self.showStatus("Committing entries")
            self.commit()
            self.modelXbrl.profileStat(_("XbrlSqlDB: insertion committed"), time.time() - startedAt)
            self.showStatus("DB insertion completed", clearAfter=5000)
        except Exception as ex:
            self.showStatus("DB insertion failed due to exception", clearAfter=5000)
            raise
    
    def insertInstance(self):
        now = datetime.datetime.now()
        entityCode = periodInstantDate = None
        # find primary model taxonomy of instance
        moduleId = None
        if self.modelXbrl.modelDocument.type in (Type.INSTANCE, Type.INLINEXBRL):
            for refDoc, ref in self.modelXbrl.modelDocument.referencesDocument.items():
                if refDoc.inDTS and ref.referenceType == "href":
                    table = self.getTable('Module', 'ModuleID', 
                                          ('TaxonomyID', 'URI'), 
                                          ('URI',), 
                                          ((None, # taxonomy ID
                                            refDoc.uri
                                            ),),
                                          checkIfExisting=True,
                                          returnExistenceStatus=True)
                    for id, URI, existenceStatus in table:
                        moduleId = id
                        break
        for cntx in self.modelXbrl.contexts.values():
            if cntx.isInstantPeriod:
                entityCode = cntx.entityIdentifier[1]
                periodInstantDate = cntx.endDatetime.date() - datetime.timedelta(1)  # convert to end date
        table = self.getTable('Instance', 'InstanceID', 
                              ('ModuleID', 'FileName', 'CompressedFileBlob',
                               'Date', 'EntityCode', 'EntityName', 'Period',
                               'EntityInternalName', 'EntityCurrency'), 
                              ('ModuleID',), 
                              ((moduleId,
                                self.modelXbrl.uri,
                                None,
                                now,
                                entityCode, 
                                None, 
                                periodInstantDate, 
                                None, 
                                None
                                ),),
                              checkIfExisting=True,
                              returnExistenceStatus=True)
        for id, moduleID, existenceStatus in table:
            self.instanceId = id
            self.instancePreviouslyInDB = existenceStatus
            break
 
    def insertDataPoints(self):
        instanceId = self.instanceId
        if self.instancePreviouslyInDB:
            self.showStatus("deleting prior data points of this report")
            # remove prior facts
            self.execute("DELETE FROM {0} WHERE {0}.InstanceID = {1}"
                         .format( self.dbTableName("dCloseTableFact"), instanceId), 
                         close=False, fetch=False)
            self.execute("DELETE FROM {0} WHERE {0}.InstanceID = {1}"
                         .format( self.dbTableName("dOpenTableSheetsRow"), instanceId), 
                         close=False, fetch=False)
            self.execute("DELETE FROM {0} WHERE {0}.InstanceID = {1}"
                         .format( self.dbTableName("dProcessingContext"), instanceId), 
                         close=False, fetch=False)
            self.execute("DELETE FROM {0} WHERE {0}.InstanceID = {1}"
                         .format( self.dbTableName("dProcessingFact"), instanceId), 
                         close=False, fetch=False)
        self.showStatus("insert data points")
        # contexts
        def dimKey(cntx, typedDim=False):
            return '|'.join(sorted("{}({})".format(dim.dimensionQname,
                                                   dim.memberQname if dim.isExplicit 
                                                   else xmlstring(dim.typedMember, stripXmlns=True) if typedDim
                                                   else '*' )
                                   for dim in cntx.qnameDims.values()))
        contextSortedDims = dict((cntx.id, dimKey(cntx))
                                 for cntx in self.modelXbrl.contexts.values()
                                 if cntx.qnameDims)
        
        def met(fact):
            return "MET({})".format(fact.qname)
        
        def metDimKey(fact):
            key = met(fact)
            if fact.contextID in contextSortedDims:
                key += '|' + contextSortedDims[fact.contextID]
            return key
            
        table = self.getTable("dProcessingContext", None,
                              ('InstanceID', 'ContextID', 'SortedDimensions', 'NotValid'),
                              ('InstanceID', 'ContextID'),
                              tuple((instanceId,
                                     cntxID,
                                     cntxDimKey,
                                     False
                                     )
                                    for cntxID, cntxDimKey in contextSortedDims.items()))
        
        # contexts with typed dimensions
        
        # dCloseFactTable
        dCloseTableFacts = []
        dProcessingFacts = []
        dFacts = []
        for f in self.modelXbrl.factsInInstance:
            cntx = f.context
            concept = f.concept
            isNumeric = isBool = isDateTime = isText = False
            if concept is not None:
                if concept.isNumeric:
                    isNumeric = True
                else:
                    baseXbrliType = concept.baseXbrliType
                    if baseXbrliType == "booleanItemType":
                        isBool = True
                    elif baseXbrliType == "dateTimeItemType": # also is dateItemType?
                        isDateTime = True
                xValue = f.xValue
            else:
                if f.isNil:
                    xValue = None
                else:
                    xValue = f.value
                    c = f.qname.localName[0]
                    if c == 'm':
                        isNumeric = True
                        # not validated, do own xValue
                        try:
                            xValue = Decimal(xValue)
                        except InvalidOperation:
                            xValue = Decimal('NaN')
                    elif c == 'd':
                        isDateTime = True
                        try:
                            xValue = dateTime(xValue, type=DATEUNION, castException=ValueError)
                        except ValueError:
                            pass
                    elif c == 'b':
                        isBool = True
                        xValue = xValue.strip()
                        if xValue in ("true", "1"):  
                            xValue = True
                        elif xValue in ("false", "0"): 
                            xValue = False
                
            isText = not (isNumeric or isBool or isDateTime)
            if cntx is not None:
                if any(dim.isTyped for dim in cntx.qnameDims.values()):
                    # typed dim in fact
                    dFacts.append((f.decimals,
                                   # factID auto generated (?)
                                   None,
                                   metDimKey(f),
                                   instanceId,
                                   cntx.entityIdentifier[1],
                                   cntx.endDatetime.date() - datetime.timedelta(1),
                                   f.unitID,
                                   xValue if isNumeric else None,
                                   xValue if isDateTime else None,
                                   xValue if isBool else None,
                                   xValue if isText else None
                                   ))
                else:
                    # no typed dim in fact
                    dFacts.append((f.decimals,
                                   # factID auto generated (?)
                                   None,
                                   metDimKey(f),
                                   instanceId,
                                   cntx.entityIdentifier[1],
                                   cntx.endDatetime.date() - datetime.timedelta(1),
                                   f.unitID,
                                   xValue if isNumeric else None,
                                   xValue if isDateTime else None,
                                   xValue if isBool else None,
                                   xValue if isText else None
                                   ))
                    dCloseTableFacts.append((instanceId,
                                              metDimKey(f),
                                              f.unitID,
                                              f.decimals,
                                              xValue if isNumeric else None,
                                              xValue if isDateTime else None,
                                              xValue if isBool else None,
                                              xValue if isText else None,
                                              None
                                              ))
                dProcessingFacts.append((instanceId,
                                         met(f),
                                         cntx.id,
                                         f.value,
                                         f.decimals,
                                         cntx.endDatetime.date() - datetime.timedelta(1),
                                         None))
        table = self.getTable("Fact", "FactID",
                              ("Decimals", "VariableID", "DataPointKey",
                               "InstanceID", "EntityID", "DatePeriodEnd", "Unit",
                               'NumericValue', 'DateTimeValue', 'BoolValue', 'TextValue'),
                              ("InstanceID", ),
                              dFacts)
        table = self.getTable("dCloseTableFact", None,
                              ('InstanceID', 'MetricDimMem', 'Unit', 'Decimals',
                               'NumericValue', 'DateTimeValue', 'BoolValue', 'TextValue',
                               'InstanceIdMetricDimMemHash'),
                              ('InstanceID', ),
                              dCloseTableFacts)
        table = self.getTable("dProcessingFact", None,
                              ('InstanceID', 'Metric', 'ContextID', 
                               'ValueTxt', 'ValueDecimal', 'ValueDate',
                               'Error'),
                              ('InstanceID', ),
                              dProcessingFacts)
        
    def loadXbrlFromDB(self, loadDBsaveToFile):
        # load from database
        modelXbrl = self.modelXbrl
        
        # find instance in DB
        instanceURI = os.path.basename(loadDBsaveToFile)
        results = self.execute("SELECT InstanceID, ModuleID FROM Instance WHERE FileName = '{}'"
                               .format(instanceURI))
        instanceId = moduleId = None
        for instanceId, moduleId in results:
            break

        # find module in DB        
        results = self.execute("SELECT URI FROM Module WHERE ModuleID = {}".format(moduleId))
        moduleURI = None
        for result in results:
            moduleURI = result[0]
            break
        
        if not instanceId or not moduleURI:
            raise XPDBException("sqlDB:MissingDTS",
                    _("The instance and module were not found for %(instanceURI)"),
                    instanceURI = instanceURI) 


        # create the instance document and resulting filing
        modelXbrl.blockDpmDBrecursion = True
        modelXbrl.modelDocument = createModelDocument(modelXbrl, 
                                                      Type.INSTANCE,
                                                      loadDBsaveToFile,
                                                      schemaRefs=[moduleURI],
                                                      isEntry=True)
        ValidateXbrlDimensions.loadDimensionDefaults(modelXbrl) # needs dimension defaults 

        # add roleRef and arcroleRef (e.g. for footnotes, if any, see inlineXbrlDocue)
        
        # facts in this instance
        factsTbl = self.execute("SELECT FactID, Decimals, VariableID, DataPointKey, EntityID, "
                                " DatePeriodEnd, Unit, NumericValue, DateTimeValue, BoolValue, TextValue "
                                "FROM Fact WHERE InstanceID = {}"
                                .format(instanceId))
        # results tuple: factId, dec, varId, dpKey, entId, datePerEnd, unit, numVal, dateVal, boolVal, textVal

        # get typed dimension values
        result = self.execute("SELECT VariableId, VariableKey FROM DataPointVariable WHERE VariableId in ({})"
                              .format(', '.join(fact[2]
                                                for fact in factsTbl
                                                if fact[2])))
        dimsTbl = dict((varId, varKey)
                       for varId, varKey in result)
        
        prefixedNamespaces = modelXbrl.prefixedNamespaces
        prefixedNamespaces["iso4217"] = XbrlConst.iso4217
        
        cntxTbl = {} # index by d
        unitTbl = {}
        
        def typedDimElt(s):
            # add xmlns into s for known qnames
            tag, angleBrkt, rest = s[1:].partition('>')
            text, angleBrkt, rest = rest.partition("<")
            qn = qname(tag, prefixedNamespaces)
            # a modelObject xml element is needed for all of the instance functions to manage the typed dim
            return addChild(modelXbrl.modelDocument, qn, text=text, appendChild=False)
        
        # contexts and facts
        for factId, dec, varId, dpKey, entId, datePerEnd, unit, numVal, dateVal, boolVal, textVal in factsTbl:
            if varId:
                if isinstance(varId, _STR_BASE):
                    varId = int(varId)  # variable table is indexed by int, but TEXT in the instance table
                dpKey = dimsTbl[varId]
            metric, sep, dims = dpKey.partition('|')
            conceptQn = qname(metric.partition('(')[2][:-1], prefixedNamespaces)
            concept = modelXbrl.qnameConcepts.get(conceptQn)
            if isinstance(datePerEnd, _STR_BASE):
                datePerEnd = datetimeValue(datePerEnd, addOneDay=True)
            if isinstance(dec, float):
                dec = int(dec)  # must be INF or integer
            cntxKey = (dims, entId, datePerEnd)
            if cntxKey in cntxTbl:
                cntxId = cntxTbl[cntxKey]
            else:
                cntxId = 'c{}'.format(len(cntxTbl) + 1)
                cntxTbl[cntxKey] = cntxId
                qnameDims = {}
                for dim in dims.split('|'):
                    dQn, sep, dVal = dim[:-1].partition('(')
                    dimQname = qname(dQn, prefixedNamespaces)
                    if dVal.startswith('<'): # typed dim
                        mem = typedDimElt(dVal)
                    else:
                        mem = qname(dVal, prefixedNamespaces)
                    qnameDims[dimQname] = DimValuePrototype(modelXbrl, None, dimQname, mem, "scenario")
                    
                modelXbrl.createContext("http://www.xbrl.org/lei",
                                         entId,
                                         'instant',
                                         None,
                                         datePerEnd,
                                         None, # no dimensional validity checking (like formula does)
                                         qnameDims, [], [],
                                         id=cntxId)
            if unit:
                if unit in unitTbl:
                    unitId = unitTbl[unit]
                else:
                    unitQn = qname(unit, prefixedNamespaces)
                    unitId = 'u{}'.format(unitQn.localName)
                    unitTbl[unit] = unitId
                    modelXbrl.createUnit([unitQn], [], id=unitId)
            else:
                unitId = None
            attrs = {"contextRef": cntxId}
            if unitId:
                attrs["unitRef"] = unitId
            if dec is not None:
                attrs["decimals"] = str(dec)  # somehow it is float from the database
            if False: # fact.isNil:
                attrs[XbrlConst.qnXsiNil] = "true"
                text = None
            elif numVal is not None:
                num = roundValue(numVal, None, dec) # round using reported decimals
                if dec is None or dec == "INF":  # show using decimals or reported format
                    dec = len(numVal.partition(".")[2])
                else: # max decimals at 28
                    dec = max( min(int(dec), 28), -28) # 2.7 wants short int, 3.2 takes regular int, don't use _INT here
                text = Locale.format(self.modelXbrl.locale, "%.*f", (dec, num))
            elif dateVal is not None:
                text = dateVal
            elif boolVal is not None:
                text = 'true' if boolVal.lower() in ('t', 'true', '1') else 'false'
            else:
                if concept.baseXsdType == "QName": # declare namespace
                    addQnameValue(modelXbrl.modelDocument, qname(textVal, prefixedNamespaces))
                text = textVal
            modelXbrl.createFact(conceptQn, attributes=attrs, text=text)
            
        # add footnotes if any
        
        # save to file
        modelXbrl.saveInstance(overrideFilepath=loadDBsaveToFile)
        modelXbrl.modelManager.showStatus(_("Saved extracted instance"), 5000)
        return modelXbrl.modelDocument
